"""
LangGraph node implementations for the K8s troubleshooting agent.

Flow: router → fetch_resources → summarize → rag_retrieve → analyze → output
Each node is a pure(ish) async function that reads/writes AgentState.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from ..config.settings import get_settings
from ..llm.factory import LLMFactory, invoke_llm_with_retry
from ..llm.prompt_templates import (
    SYSTEM_PROMPT,
    build_analysis_prompt,
    build_rag_only_prompt,
)
from ..tools.kubectl_wrapper import KubectlWrapper
from ..tools.rag_tools import RAGRetriever
from ..tools.summarizer import ResourceSummarizer
from .state import AgentState

logger = logging.getLogger(__name__)

# ─────────────────────────── Shared singletons ──────────────────────────────

_kubectl: Optional[KubectlWrapper] = None
_summarizer: Optional[ResourceSummarizer] = None
_rag: Optional[RAGRetriever] = None


def _get_kubectl() -> KubectlWrapper:
    global _kubectl
    if _kubectl is None:
        _kubectl = KubectlWrapper()
    return _kubectl


def _get_summarizer() -> ResourceSummarizer:
    global _summarizer
    settings = get_settings()
    if _summarizer is None:
        _summarizer = ResourceSummarizer(
            max_log_lines=settings.max_log_lines,
            token_budget=settings.token_budget,
        )
    return _summarizer


def _get_rag() -> RAGRetriever:
    global _rag
    if _rag is None:
        _rag = RAGRetriever()
    return _rag


# ─────────────────────────── Router Node ────────────────────────────────────

CLUSTER_KEYWORDS = re.compile(
    r"\b(pod|deploy|service|node|pvc|ingress|namespace|crash|error|fail|stuck|"
    r"pending|running|restart|evict|oom|image|pull|log|event|hpa|job|stateful|"
    r"daemonset|configmap|secret|endpoint|replica)\b",
    re.IGNORECASE,
)

RAG_KEYWORDS = re.compile(
    r"\b(how|what|why|explain|documentation|docs|concept|best.practice|recommend|"
    r"difference|compare|example|guide|tutorial|architecture)\b",
    re.IGNORECASE,
)


async def router_node(state: AgentState) -> AgentState:
    """
    Decides whether to fetch live cluster data, use RAG, or both.
    Also probes cluster connectivity.
    """
    query = state.get("query", "")
    settings = get_settings()

    logger.debug("Router: analyzing query: %s", query)

    needs_cluster = bool(CLUSTER_KEYWORDS.search(query)) or bool(state.get("resource_name"))
    needs_rag = bool(RAG_KEYWORDS.search(query)) or not needs_cluster

    # Always try RAG for better context
    needs_rag = True

    # Probe cluster connectivity
    cluster_reachable = False
    if needs_cluster:
        try:
            kubectl = _get_kubectl()
            cluster_reachable = await asyncio.wait_for(
                kubectl.probe_cluster(), timeout=5.0
            )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("Cluster probe failed: %s", exc)
            cluster_reachable = False

    if not cluster_reachable and needs_cluster:
        logger.info("Cluster unreachable — switching to RAG-only mode")

    return {
        **state,
        "needs_cluster_data": needs_cluster and cluster_reachable,
        "needs_rag": needs_rag,
        "cluster_reachable": cluster_reachable,
        "steps_taken": state.get("steps_taken", []) + ["router"],
        "errors": state.get("errors", []),
        "warnings": state.get("warnings", []) + (
            [] if cluster_reachable or not needs_cluster else
            ["Cluster unreachable — using documentation-based guidance only"]
        ),
    }


# ─────────────────────────── Resource Fetcher Node ──────────────────────────

async def fetch_resources_node(state: AgentState) -> AgentState:
    """
    Fetches relevant Kubernetes resources in parallel.
    Respects state.needs_cluster_data flag.
    """
    if not state.get("needs_cluster_data"):
        return {**state, "steps_taken": state.get("steps_taken", []) + ["fetch_resources(skipped)"]}

    kubectl = _get_kubectl()
    namespace = state.get("namespace", "default")
    resource_name = state.get("resource_name")
    resource_type = state.get("resource_type", "pod").lower()

    errors = list(state.get("errors", []))
    warnings = list(state.get("warnings", []))

    # Determine what to fetch based on resource type and query
    query = state.get("query", "").lower()

    # --- Always fetch events ---
    fetch_tasks = {
        "events": kubectl.get_events(namespace=namespace, resource_name=resource_name),
    }

    # --- Fetch specific resource type ---
    if resource_type in ("pod", "pods") or any(kw in query for kw in ["pod", "crash", "log", "container"]):
        if resource_name:
            fetch_tasks["pods"] = kubectl.get_resource("pods", resource_name, namespace)
            fetch_tasks["logs"] = kubectl.get_logs(resource_name, namespace, tail=100)
            fetch_tasks["prev_logs"] = kubectl.get_logs(resource_name, namespace, tail=50, previous=True)
        else:
            fetch_tasks["pods"] = kubectl.get_pods(namespace)

    if resource_type in ("deployment", "deploy") or "deploy" in query:
        if resource_name:
            fetch_tasks["deployments"] = kubectl.get_resource("deployments", resource_name, namespace)
        else:
            fetch_tasks["deployments"] = kubectl.get_deployments(namespace)

    if resource_type in ("node", "nodes") or "node" in query:
        fetch_tasks["nodes"] = kubectl.get_nodes()

    if resource_type in ("service", "svc") or "service" in query or "endpoint" in query:
        if resource_name:
            fetch_tasks["services"] = kubectl.get_resource("services", resource_name, namespace)
        else:
            fetch_tasks["services"] = kubectl.get_services(namespace)

    if resource_type in ("pvc", "persistentvolumeclaim") or "pvc" in query or "volume" in query:
        if resource_name:
            fetch_tasks["pvcs"] = kubectl.get_resource("persistentvolumeclaims", resource_name, namespace)
        else:
            fetch_tasks["pvcs"] = kubectl.get_pvcs(namespace)

    # If nothing specific, fetch pods (most common cause)
    if "pods" not in fetch_tasks and not any(k in fetch_tasks for k in ["deployments", "nodes", "services"]):
        fetch_tasks["pods"] = kubectl.get_pods(namespace)

    # --- Execute all fetches in parallel ---
    logger.info("Fetching %d resource types in parallel...", len(fetch_tasks))
    task_keys = list(fetch_tasks.keys())
    results = await asyncio.gather(*fetch_tasks.values(), return_exceptions=True)
    result_map = dict(zip(task_keys, results))

    # --- Process results ---
    raw_pod_data = []
    raw_deployment_data = []
    raw_node_data = []
    raw_service_data = []
    raw_pvc_data = []
    raw_logs = {}
    raw_prev_logs = {}

    for key, result in result_map.items():
        if isinstance(result, Exception):
            errors.append(f"Failed to fetch {key}: {result}")
            continue
        if not result.success:
            if result.is_not_found:
                warnings.append(f"Resource not found: {key}")
            elif result.is_timeout:
                warnings.append(f"kubectl timeout for {key}")
            else:
                warnings.append(f"Failed to fetch {key}: {result.stderr or result.error_message}")
            continue

        if key == "pods":
            data = result.parsed or {}
            if data.get("kind") == "Pod":
                raw_pod_data = [data]
            elif data.get("kind") == "PodList":
                raw_pod_data = data.get("items", [])

        elif key == "deployments":
            data = result.parsed or {}
            if data.get("kind") == "Deployment":
                raw_deployment_data = [data]
            elif data.get("kind") == "DeploymentList":
                raw_deployment_data = data.get("items", [])

        elif key == "nodes":
            data = result.parsed or {}
            if data.get("kind") == "Node":
                raw_node_data = [data]
            elif data.get("kind") == "NodeList":
                raw_node_data = data.get("items", [])

        elif key == "services":
            data = result.parsed or {}
            if data.get("kind") == "Service":
                raw_service_data = [data]
            elif data.get("kind") == "ServiceList":
                raw_service_data = data.get("items", [])

        elif key == "pvcs":
            data = result.parsed or {}
            if data.get("kind") == "PersistentVolumeClaim":
                raw_pvc_data = [data]
            elif data.get("kind") == "PersistentVolumeClaimList":
                raw_pvc_data = data.get("items", [])

        elif key == "logs" and resource_name:
            raw_logs[resource_name] = result.stdout

        elif key == "prev_logs" and resource_name:
            raw_prev_logs[resource_name] = result.stdout

        elif key == "events":
            pass  # Handled below

    # Events
    events_result = result_map.get("events")
    raw_events_json = None
    if events_result and not isinstance(events_result, Exception) and events_result.success:
        raw_events_json = events_result.parsed

    logger.info(
        "Fetched: %d pods, %d deployments, %d nodes, %d services, %d pvcs",
        len(raw_pod_data), len(raw_deployment_data), len(raw_node_data),
        len(raw_service_data), len(raw_pvc_data),
    )

    return {
        **state,
        "raw_pod_data": raw_pod_data,
        "raw_deployment_data": raw_deployment_data,
        "raw_node_data": raw_node_data,
        "raw_service_data": raw_service_data,
        "raw_pvc_data": raw_pvc_data,
        "raw_events_json": raw_events_json,
        "raw_logs": raw_logs,
        "raw_prev_logs": raw_prev_logs,
        "errors": errors,
        "warnings": warnings,
        "steps_taken": state.get("steps_taken", []) + ["fetch_resources"],
    }


# ─────────────────────────── Summarizer Node ────────────────────────────────

async def summarize_node(state: AgentState) -> AgentState:
    """
    Compresses raw kubectl data into a token-efficient structured context.
    This is the core token optimization step (95% reduction).
    """
    summarizer = _get_summarizer()
    query = state.get("query", "")
    namespace = state.get("namespace", "default")

    # Build pod data tuples (pod_json, log, prev_log)
    pod_data = []
    for pod_json in (state.get("raw_pod_data") or []):
        pod_name = pod_json.get("metadata", {}).get("name", "")
        log = state.get("raw_logs", {}).get(pod_name)
        prev_log = state.get("raw_prev_logs", {}).get(pod_name)
        pod_data.append((pod_json, log, prev_log))

    structured = summarizer.build_context(
        query=query,
        namespace=namespace,
        pod_data=pod_data if pod_data else None,
        deployment_data=state.get("raw_deployment_data") or None,
        node_data=state.get("raw_node_data") or None,
        service_data=state.get("raw_service_data") or None,
        pvc_data=state.get("raw_pvc_data") or None,
        events_json=state.get("raw_events_json"),
        cluster_reachable=state.get("cluster_reachable", True),
        warnings=state.get("warnings", []),
    )

    logger.info("Summarized context: %d tokens", structured.token_count)

    return {
        **state,
        "structured_context": structured.model_dump(),
        "token_count": structured.token_count,
        "steps_taken": state.get("steps_taken", []) + ["summarize"],
    }


# ─────────────────────────── RAG Retrieval Node ─────────────────────────────

async def rag_retrieve_node(state: AgentState) -> AgentState:
    """Retrieves relevant K8s documentation chunks for the query."""
    if not state.get("needs_rag"):
        return {**state, "steps_taken": state.get("steps_taken", []) + ["rag(skipped)"]}

    query = state.get("query", "")
    rag = _get_rag()

    # Run in executor to avoid blocking (embedding can be slow)
    loop = asyncio.get_event_loop()
    try:
        rag_context = await asyncio.wait_for(
            loop.run_in_executor(None, rag.retrieve, query),
            timeout=10.0,
        )
        logger.debug("RAG retrieved %d chars", len(rag_context))
    except asyncio.TimeoutError:
        rag_context = None
        logger.warning("RAG retrieval timed out")
    except Exception as exc:
        rag_context = None
        logger.warning("RAG retrieval failed: %s", exc)

    return {
        **state,
        "rag_context": rag_context,
        "steps_taken": state.get("steps_taken", []) + ["rag_retrieve"],
    }


# ─────────────────────────── Analyzer Node ──────────────────────────────────

async def analyze_node(state: AgentState) -> AgentState:
    """
    Sends structured context to the LLM for diagnosis.
    Returns structured JSON diagnosis.
    """
    settings = get_settings()

    structured_context = state.get("structured_context", {})
    rag_context = state.get("rag_context")
    cluster_reachable = state.get("cluster_reachable", True)
    query = state.get("query", "")

    # Build appropriate prompt
    if cluster_reachable and structured_context:
        user_prompt = build_analysis_prompt(structured_context, rag_context)
    else:
        # RAG-only mode
        rag_context = rag_context or _get_rag().retrieve(query)
        user_prompt = build_rag_only_prompt(query, rag_context)

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    logger.info("Calling LLM: provider=%s, model=%s", settings.provider.value, settings.model)

    try:
        llm = LLMFactory.create(settings)

        # Run in executor (LangChain sync invoke)
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: invoke_llm_with_retry(llm, messages),
        )

        raw_content = response.content if hasattr(response, "content") else str(response)

        # Parse JSON from response
        diagnosis = _extract_json(raw_content)

        if not diagnosis:
            # Fallback structured response
            diagnosis = {
                "diagnosis": {
                    "root_cause": "Unable to parse LLM response",
                    "confidence": 0.0,
                    "affected_resources": [],
                    "severity": "unknown",
                    "category": "other",
                },
                "analysis": raw_content[:500],
                "suggestions": [],
                "additional_checks": ["Check LLM configuration and API key"],
                "estimated_fix_time": "unknown",
            }

        return {
            **state,
            "diagnosis": diagnosis,
            "llm_raw_response": raw_content,
            "steps_taken": state.get("steps_taken", []) + ["analyze"],
        }

    except ValueError as exc:
        # API key / config errors
        error_msg = str(exc)
        return {
            **state,
            "diagnosis": {
                "diagnosis": {
                    "root_cause": "LLM configuration error",
                    "confidence": 0.0,
                    "affected_resources": [],
                    "severity": "high",
                    "category": "other",
                },
                "analysis": error_msg,
                "suggestions": [
                    {
                        "description": "Fix LLM configuration",
                        "commands": [],
                        "priority": "high",
                        "expected_output": "Set the appropriate API key environment variable",
                    }
                ],
                "additional_checks": [],
                "estimated_fix_time": "quick (< 5 min)",
            },
            "errors": state.get("errors", []) + [error_msg],
            "steps_taken": state.get("steps_taken", []) + ["analyze(failed)"],
        }
    except Exception as exc:
        logger.exception("LLM call failed: %s", exc)
        return {
            **state,
            "diagnosis": None,
            "errors": state.get("errors", []) + [f"LLM error: {exc}"],
            "steps_taken": state.get("steps_taken", []) + ["analyze(error)"],
        }


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract JSON from LLM response, handling markdown code blocks."""
    if not text:
        return None

    # Try direct JSON parse
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    patterns = [
        r"```json\s*\n?(.*?)\n?```",
        r"```\s*\n?(.*?)\n?```",
        r"\{.*\}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1 if "(" in pattern else 0).strip())
            except (json.JSONDecodeError, IndexError):
                continue

    # Try to find JSON object anywhere in text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


# ─────────────────────────── Suggest Commands Node ──────────────────────────

async def suggest_commands_node(state: AgentState) -> AgentState:
    """
    Post-processes diagnosis to ensure all suggested commands are read-only.
    Strips any accidentally mutating commands.
    """
    from ..config.blocklist import is_command_safe

    diagnosis = state.get("diagnosis")
    if not diagnosis:
        return state

    suggestions = diagnosis.get("suggestions", [])
    filtered_suggestions = []

    for sugg in suggestions:
        safe_commands = []
        for cmd in sugg.get("commands", []):
            is_safe, reason = is_command_safe(cmd)
            if is_safe:
                safe_commands.append(cmd)
            else:
                logger.warning("Stripped unsafe command from suggestions: %s | %s", cmd, reason)
        sugg["commands"] = safe_commands
        filtered_suggestions.append(sugg)

    diagnosis["suggestions"] = filtered_suggestions

    return {
        **state,
        "diagnosis": diagnosis,
        "steps_taken": state.get("steps_taken", []) + ["suggest_commands"],
    }
