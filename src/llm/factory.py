"""
LLM factory — creates the appropriate LangChain LLM based on provider config.
Supports: OpenAI, Google Gemini, Anthropic Claude, Ollama (local).
"""

import logging
from functools import lru_cache
from typing import Any, Optional

from ..config.settings import LLMProvider, Settings, get_settings

logger = logging.getLogger(__name__)


def get_llm(settings: Optional[Settings] = None, **kwargs):
    """
    Factory function: returns a configured LangChain LLM instance.

    The LLM inference runs on the provider's cloud (OpenAI/Gemini/Anthropic).
    Only Ollama runs locally.
    """
    s = settings or get_settings()
    return LLMFactory.create(s, **kwargs)


class LLMFactory:
    """Creates LangChain-compatible LLM instances for each provider."""

    @staticmethod
    def create(settings: Settings, **kwargs) -> Any:
        """Create and return the appropriate LLM."""
        provider = settings.provider
        model = settings.model

        logger.info("Creating LLM: provider=%s, model=%s", provider.value, model)

        if provider == LLMProvider.OPENAI:
            return LLMFactory._create_openai(settings, model, **kwargs)
        elif provider == LLMProvider.GEMINI:
            return LLMFactory._create_gemini(settings, model, **kwargs)
        elif provider == LLMProvider.CLAUDE:
            return LLMFactory._create_claude(settings, model, **kwargs)
        elif provider == LLMProvider.OLLAMA:
            return LLMFactory._create_ollama(settings, model, **kwargs)
        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")

    @staticmethod
    def _create_openai(settings: Settings, model: str, **kwargs):
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError("langchain-openai not installed. Run: pip install langchain-openai")

        if not settings.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY environment variable not set. "
                "Get your key at https://platform.openai.com/api-keys"
            )

        return ChatOpenAI(
            model=model,
            api_key=settings.openai_api_key,
            temperature=settings.temperature,
            max_tokens=min(4096, settings.token_budget // 2),
            request_timeout=30,
            max_retries=3,
            **kwargs,
        )

    @staticmethod
    def _create_gemini(settings: Settings, model: str, **kwargs):
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            raise ImportError(
                "langchain-google-genai not installed. Run: pip install langchain-google-genai"
            )

        if not settings.gemini_api_key:
            raise ValueError(
                "GEMINI_API_KEY environment variable not set. "
                "Get your key at https://aistudio.google.com/app/apikey"
            )

        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=settings.gemini_api_key,
            temperature=settings.temperature,
            max_output_tokens=min(4096, settings.token_budget // 2),
            convert_system_message_to_human=True,  # Gemini quirk
            **kwargs,
        )

    @staticmethod
    def _create_claude(settings: Settings, model: str, **kwargs):
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise ImportError(
                "langchain-anthropic not installed. Run: pip install langchain-anthropic"
            )

        if not settings.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY environment variable not set. "
                "Get your key at https://console.anthropic.com/"
            )

        return ChatAnthropic(
            model=model,
            api_key=settings.anthropic_api_key,
            temperature=settings.temperature,
            max_tokens=min(4096, settings.token_budget // 2),
            **kwargs,
        )

    @staticmethod
    def _create_ollama(settings: Settings, model: str, **kwargs):
        try:
            from langchain_community.chat_models import ChatOllama
        except ImportError:
            raise ImportError(
                "langchain-community not installed. Run: pip install langchain-community"
            )

        logger.info("Using local Ollama at %s with model %s", settings.ollama_url, model)
        return ChatOllama(
            base_url=settings.ollama_url,
            model=model,
            temperature=settings.temperature,
            **kwargs,
        )

    @staticmethod
    def probe_provider(settings: Settings) -> tuple[bool, str]:
        """
        Quick check if the configured provider is reachable.
        Returns (is_available, message).
        """
        try:
            llm = LLMFactory.create(settings)
            # Send minimal test message
            from langchain_core.messages import HumanMessage
            response = llm.invoke([HumanMessage(content="Reply with: OK")])
            return True, f"Provider {settings.provider.value} is available"
        except Exception as exc:
            return False, f"Provider {settings.provider.value} unavailable: {exc}"

    @staticmethod
    def list_recommended_models(provider: LLMProvider) -> list[str]:
        """Return recommended models for each provider."""
        models = {
            LLMProvider.OPENAI: [
                "gpt-4o-mini",          # Best cost/performance
                "gpt-4o",               # Best quality
                "gpt-3.5-turbo",        # Cheapest
            ],
            LLMProvider.GEMINI: [
                "gemini-1.5-flash",     # Fast and cheap
                "gemini-1.5-pro",       # Better quality
                "gemini-2.0-flash",     # Latest fast
            ],
            LLMProvider.CLAUDE: [
                "claude-3-5-sonnet-20241022",   # Best quality
                "claude-3-5-haiku-20241022",    # Faster/cheaper
                "claude-3-opus-20240229",       # Most capable
            ],
            LLMProvider.OLLAMA: [
                "llama3.1",
                "qwen2.5",
                "mistral",
                "codellama",
            ],
        }
        return models.get(provider, [])


def invoke_llm_with_retry(
    llm,
    messages: list,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """
    Invoke LLM with exponential backoff retry on rate limits.
    """
    import asyncio
    import random
    import time

    for attempt in range(max_retries):
        try:
            return llm.invoke(messages)
        except Exception as exc:
            error_str = str(exc).lower()

            # Rate limit errors
            if any(kw in error_str for kw in ["rate_limit", "ratelimit", "429", "quota"]):
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    logger.warning("Rate limit hit, retrying in %.1fs (attempt %d/%d)", delay, attempt + 1, max_retries)
                    time.sleep(delay)
                    continue

            # API key errors
            if any(kw in error_str for kw in ["api_key", "authentication", "unauthorized", "401"]):
                raise ValueError(
                    f"API key error: {exc}\n"
                    "Check that your API key environment variable is set correctly."
                )

            # Connection errors
            if any(kw in error_str for kw in ["connection", "timeout", "connect"]):
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning("Connection error, retrying in %.1fs: %s", delay, exc)
                    time.sleep(delay)
                    continue

            raise

    raise RuntimeError(f"LLM call failed after {max_retries} retries")
