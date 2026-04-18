"""
Pydantic settings with full validation for k8s-ai-support.
All configuration via environment variables with sensible defaults.
"""

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(str, Enum):
    OPENAI = "openai"
    GEMINI = "gemini"
    CLAUDE = "claude"
    OLLAMA = "ollama"


class OutputFormat(str, Enum):
    JSON = "json"
    YAML = "yaml"
    TABLE = "table"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="K8S_AI_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM Configuration
    provider: LLMProvider = Field(default=LLMProvider.OPENAI, description="LLM provider")
    model: str = Field(default="gpt-4o-mini", description="Model name for the selected provider")
    ollama_url: str = Field(default="http://localhost:11434", description="Ollama server URL")
    temperature: float = Field(default=0.1, ge=0.0, le=2.0, description="LLM temperature")

    # API Keys (not prefixed, standard env vars)
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    gemini_api_key: Optional[str] = Field(default=None, alias="GEMINI_API_KEY")
    anthropic_api_key: Optional[str] = Field(default=None, alias="ANTHROPIC_API_KEY")

    # Token Budget
    token_budget: int = Field(default=8000, ge=1000, le=128000, description="Max tokens per LLM call")
    max_log_lines: int = Field(default=10, ge=5, le=100, description="Max log lines to include")
    rag_top_k: int = Field(default=3, ge=1, le=10, description="Top K RAG chunks to retrieve")
    rag_chunk_size: int = Field(default=512, ge=128, le=2048, description="RAG chunk size in tokens")

    # Kubernetes
    kubeconfig: Optional[str] = Field(default=None, description="Path to kubeconfig file")
    kubectl_timeout: int = Field(default=10, ge=1, le=60, description="kubectl command timeout (seconds)")
    default_namespace: str = Field(default="default", description="Default Kubernetes namespace")

    # Logging
    log_level: LogLevel = Field(default=LogLevel.INFO, description="Log level")
    log_format: str = Field(default="json", description="Log format: json or text")

    # MCP Server
    mcp_mode: bool = Field(default=False, description="Run as MCP server")
    mcp_rate_limit: int = Field(default=10, ge=1, le=100, description="MCP requests per minute")

    # Cache
    cache_dir: Path = Field(
        default=Path.home() / ".cache" / "k8s-ai",
        description="Cache directory for embeddings and docs",
    )

    # RAG / Embeddings
    embedding_model: str = Field(
        default="BAAI/bge-small-en-v1.5",
        description="HuggingFace embedding model (no API call needed)",
    )
    vector_store_type: str = Field(default="chroma", description="Vector store: chroma or faiss")
    k8s_docs_version: str = Field(default="1.35", description="K8s docs version for RAG")

    # Output
    output_format: OutputFormat = Field(default=OutputFormat.TABLE, description="Default output format")
    verbose: bool = Field(default=False, description="Verbose output")

    @field_validator("kubeconfig", mode="before")
    @classmethod
    def resolve_kubeconfig(cls, v: Optional[str]) -> Optional[str]:
        if v:
            return v
        # Check KUBECONFIG env var
        env_kc = os.environ.get("KUBECONFIG")
        if env_kc:
            return env_kc
        # Default location
        default = Path.home() / ".kube" / "config"
        if default.exists():
            return str(default)
        return None

    @field_validator("cache_dir", mode="before")
    @classmethod
    def ensure_cache_dir(cls, v) -> Path:
        path = Path(v) if not isinstance(v, Path) else v
        path.mkdir(parents=True, exist_ok=True)
        return path

    @model_validator(mode="after")
    def validate_api_keys(self) -> "Settings":
        """Validate that at least one API key is set (unless using Ollama)."""
        if self.provider == LLMProvider.OLLAMA:
            return self

        key_map = {
            LLMProvider.OPENAI: self.openai_api_key,
            LLMProvider.GEMINI: self.gemini_api_key,
            LLMProvider.CLAUDE: self.anthropic_api_key,
        }

        # Auto-detect provider from available keys
        if self.provider in key_map and not key_map[self.provider]:
            # Try to find any available key
            for provider, key in key_map.items():
                if key:
                    self.provider = provider
                    # Set default model for detected provider
                    if self.model == "gpt-4o-mini" and provider != LLMProvider.OPENAI:
                        model_defaults = {
                            LLMProvider.GEMINI: "gemini-1.5-flash",
                            LLMProvider.CLAUDE: "claude-3-5-sonnet-20241022",
                        }
                        self.model = model_defaults.get(provider, self.model)
                    return self

            # No keys found — will fall back to Ollama or error at runtime
            import warnings
            warnings.warn(
                f"No API key found for provider '{self.provider}'. "
                "Set OPENAI_API_KEY, GEMINI_API_KEY, or ANTHROPIC_API_KEY. "
                "Falling back to Ollama if available.",
                UserWarning,
                stacklevel=2,
            )
            self.provider = LLMProvider.OLLAMA

        return self

    @property
    def effective_kubeconfig_args(self) -> list[str]:
        """Return kubeconfig arguments for kubectl."""
        if self.kubeconfig:
            return ["--kubeconfig", self.kubeconfig]
        return []

    def model_defaults_for_provider(self) -> str:
        """Return default model name for the configured provider."""
        defaults = {
            LLMProvider.OPENAI: "gpt-4o-mini",
            LLMProvider.GEMINI: "gemini-1.5-flash",
            LLMProvider.CLAUDE: "claude-3-5-sonnet-20241022",
            LLMProvider.OLLAMA: "llama3.1",
        }
        return defaults.get(self.provider, self.model)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear settings cache (useful for testing)."""
    get_settings.cache_clear()
