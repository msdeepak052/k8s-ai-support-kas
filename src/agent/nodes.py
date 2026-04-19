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

    logger.debug("[ROUTER] query=%r", query)
    logger.debug("[ROUTER] explicit resource_name=%r, resource_type=%r",
                 state.get("resource_name"), state.get("resource_type"))

    _cluster_kw_hit = bool(CLUSTER_KEYWORDS.search(query))
    _rag_kw_hit = bool(RAG_KEYWORDS.search(query))
    logger.debug("[ROUTER] cluster_keywords_match=%s, rag_keywords_match=%s",
                 _cluster_kw_hit, _rag_kw_hit)

    needs_cluster = _cluster_kw_hit or bool(state.get("resource_name"))
    needs_rag = bool(RAG_KEYWORDS.search(query)) or not needs_cluster

    # Always try RAG for better context
    needs_rag = True

    logger.debug("[ROUTER] needs_cluster=%s, needs_rag=%s (rag always=True)", needs_cluster, needs_rag)

    # Probe cluster connectivity
    cluster_reachable = False
    if needs_cluster:
        logger.debug("[ROUTER] probing cluster connectivity (timeout=5s)...")
        try:
            kubectl = _get_kubectl()
            cluster_reachable = await asyncio.wait_for(
                kubectl.probe_cluster(), timeout=5.0
            )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("Cluster probe failed: %s", exc)
            cluster_reachable = False

    logger.debug("[ROUTER] cluster_reachable=%s", cluster_reachable)

    if not cluster_reachable and needs_cluster:
        logger.info("Cluster unreachable — switching to RAG-only mode")

    logger.debug("[ROUTER] final route → needs_cluster_data=%s, needs_rag=%s",
                 needs_cluster and cluster_reachable, needs_rag)

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
    namespace = state.get("namespace") or "default"
    resource_name = state.get("resource_name") or None
    resource_type = (state.get("resource_type") or "pod").lower()

    errors = list(state.get("errors", []))
    warnings = list(state.get("warnings", []))

    # Determine what to fetch based on resource type and query
    query = (state.get("query") or "").lower()

    logger.debug("[FETCH] namespace=%r, resource_name=%r (from -r flag), resource_type=%r",
                 namespace, resource_name, resource_type)

    # --- Auto-detect resource name from query when -r flag is absent ---
    # e.g. "why is crash-loop-pod crashing?" → resource_name = "crash-loop-pod"
    # Kubernetes names are lowercase alphanumeric + hyphens. We look for tokens
    # that look like real object names (at least one hyphen, min 5 chars) and
    # exclude common error-message phrases that happen to look like names.
    _auto_detected_name = False
    if not resource_name:
        _SKIP_PHRASES = {
            # error-state labels that look like names but aren't
            "crash-loop", "crash-loop-back", "crash-loop-back-off",
            "back-off", "image-pull", "image-pull-back-off", "err-image-pull",
            "non-zero", "read-only", "exit-code", "init-container",
            "run-as", "node-not-ready", "out-of-memory", "oom-killed",
            "liveness-probe", "readiness-probe", "post-start", "pre-stop",
        }
        _candidates = re.findall(r'\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b', query)
        logger.debug("[FETCH] name-like tokens in query: %s", _candidates)
        for _m in _candidates:
            if _m not in _SKIP_PHRASES and len(_m) >= 5:
                resource_name = _m
                _auto_detected_name = True
                logger.debug("[FETCH] auto-detected resource_name=%r from query", resource_name)
                break
        if not _auto_detected_name:
            logger.debug("[FETCH] no resource name auto-detected — will use namespace-wide list")

    # --- Always fetch events ---
    fetch_tasks = {
        "events": kubectl.get_events(namespace=namespace, resource_name=resource_name),
    }

    # --- Fetch specific resource type ---
    _is_pod_context = (
        resource_type in ("pod", "pods")
        or any(kw in query for kw in ["pod", "crash", "log", "container", "oom", "memory", "restart"])
    )
    if _is_pod_context:
        if resource_name:
            fetch_tasks["pods"] = kubectl.get_resource("pods", resource_name, namespace)
            fetch_tasks["logs"] = kubectl.get_logs(resource_name, namespace, tail=100)
            fetch_tasks["prev_logs"] = kubectl.get_logs(resource_name, namespace, tail=50, previous=True)
            # Live resource metrics — tells LLM actual usage vs limits right now
            fetch_tasks["top_pod"] = kubectl.top_pod(resource_name, namespace)
        else:
            fetch_tasks["pods"] = kubectl.get_pods(namespace)

    # Always fetch node metrics when cluster data is needed — useful for OOM / scheduling issues
    fetch_tasks["top_nodes"] = kubectl.top_nodes()

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
    logger.debug("[FETCH] planned tasks: %s", list(fetch_tasks.keys()))
    logger.info("Fetching %d resource types in parallel...", len(fetch_tasks))
    task_keys = list(fetch_tasks.keys())
    results = await asyncio.gather(*fetch_tasks.values(), return_exceptions=True)
    result_map = dict(zip(task_keys, results))

    # Log per-task outcomes so it's clear what arrived and what failed
    for _k, _r in result_map.items():
        if isinstance(_r, Exception):
            logger.debug("[FETCH] %-18s → EXCEPTION: %s", _k, _r)
        elif _r.success:
            logger.debug("[FETCH] %-18s → OK       (%d chars stdout)", _k, len(_r.stdout))
        else:
            _detail = (_r.stderr or _r.error_message or "")[:120]
            logger.debug("[FETCH] %-18s → FAIL     rc=%s  %s", _k, _r.return_code, _detail)

    # --- Auto-fetch logs for failing pods when no specific resource was named ---
    # Without -r flag we only have the pod list. A second pass fetches logs for
    # pods in bad states so the LLM sees actual error messages, not just status.
    _FAILING_REASONS = {
        "CrashLoopBackOff", "Error", "OOMKilled",
        "CreateContainerConfigError", "ErrImagePull", "ImagePullBackOff",
        "PostStartHookError", "PreStopHookError",
    }
    if not resource_name and "pods" in result_map:
        pods_result = result_map["pods"]
        if not isinstance(pods_result, Exception) and pods_result.success:
            pod_list = pods_result.parsed or {}
            items = pod_list.get("items", []) if pod_list.get("kind") == "PodList" else [pod_list]
            failing_pod_names = []
            for pod in items:
                cs_list = pod.get("status", {}).get("containerStatuses", [])
                for cs in cs_list:
                    waiting_reason = cs.get("state", {}).get("waiting", {}).get("reason", "")
                    last_exit = cs.get("lastState", {}).get("terminated", {})
                    if waiting_reason in _FAILING_REASONS or last_exit:
                        pname = pod.get("metadata", {}).get("name", "")
                        if pname:
                            failing_pod_names.append(pname)
                        break

            logger.debug("[FETCH] failing pods detected from pod list: %s", failing_pod_names)

            if failing_pod_names:
                log_tasks: Dict[str, Any] = {}
                for pname in failing_pod_names[:3]:   # cap at 3 pods
                    log_tasks[f"logs__{pname}"] = kubectl.get_logs(pname, namespace, tail=80)
                    log_tasks[f"prev__{pname}"] = kubectl.get_logs(pname, namespace, tail=80, previous=True)

                logger.info("Auto-fetching logs for %d failing pod(s): %s",
                            len(failing_pod_names[:3]), failing_pod_names[:3])
                logger.debug("[FETCH] auto-fetch log tasks: %s", list(log_tasks.keys()))
                log_keys = list(log_tasks.keys())
                log_results = await asyncio.gather(*log_tasks.values(), return_exceptions=True)
                for lk, lr in zip(log_keys, log_results):
                    if isinstance(lr, Exception) or not lr.success:
                        continue
                    if lk.startswith("logs__"):
                        result_map[lk] = lr   # store so loop below picks it up
                    elif lk.startswith("prev__"):
                        result_map[lk] = lr

    # --- Process results ---
    raw_pod_data = []
    raw_deployment_data = []
    raw_node_data = []
    raw_service_data = []
    raw_pvc_data = []
    raw_logs = {}
    raw_prev_logs = {}
    raw_top_pod_output: Optional[str] = None
    raw_top_nodes_output: Optional[str] = None

    # Keys that are best-effort — silently skip on failure (metrics-server may not be installed,
    # pod may be mid-restart, or cluster may not have the metrics API enabled).
    _OPTIONAL_KEYS = {"top_pod", "top_nodes"}

    for key, result in result_map.items():
        if isinstance(result, Exception):
            if key in _OPTIONAL_KEYS:
                logger.debug("[FETCH] %s unavailable (exception): %s", key, result)
            else:
                errors.append(f"Failed to fetch {key}: {result}")
            continue
        if not result.success:
            if key in _OPTIONAL_KEYS:
                logger.debug("[FETCH] %s unavailable (rc=%s): %s",
                             key, result.return_code,
                             (result.stderr or result.error_message or "")[:120])
            elif result.is_not_found:
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

        elif key.startswith("logs__"):
            pod_name = key[6:]
            raw_logs[pod_name] = result.stdout

        elif key.startswith("prev__"):
            pod_name = key[6:]
            raw_prev_logs[pod_name] = result.stdout

        elif key == "top_pod":
            raw_top_pod_output = result.stdout.strip()
            logger.debug("[FETCH] top_pod output: %s", raw_top_pod_output[:120])

        elif key == "top_nodes":
            raw_top_nodes_output = result.stdout.strip()
            logger.debug("[FETCH] top_nodes output: %s", raw_top_nodes_output[:200])

        elif key == "events":
            pass  # Handled below

    # Events
    events_result = result_map.get("events")
    raw_events_json = None
    if events_result and not isinstance(events_result, Exception) and events_result.success:
        raw_events_json = events_result.parsed

    # --- Summary of what was collected ---
    logger.debug("[FETCH] raw_logs keys: %s", list(raw_logs.keys()))
    for _pod, _txt in raw_logs.items():
        logger.debug("[FETCH] log[%s] = %d chars", _pod, len(_txt or ""))
    logger.debug("[FETCH] raw_prev_logs keys: %s", list(raw_prev_logs.keys()))
    for _pod, _txt in raw_prev_logs.items():
        logger.debug("[FETCH] prev_log[%s] = %d chars", _pod, len(_txt or ""))
    logger.debug("[FETCH] top_pod=%s, top_nodes=%s",
                 bool(raw_top_pod_output), bool(raw_top_nodes_output))

    logger.info(
        "Fetched: %d pods, %d deployments, %d nodes, %d services, %d pvcs",
        len(raw_pod_data), len(raw_deployment_data), len(raw_node_data),
        len(raw_service_data), len(raw_pvc_data),
    )
    logger.debug("[FETCH] pod names collected: %s",
                 [p.get("metadata", {}).get("name") for p in raw_pod_data])

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
        "raw_top_pod_output": raw_top_pod_output,
        "raw_top_nodes_output": raw_top_nodes_output,
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
        logger.debug(
            "[SUMMARIZE] pod=%-40s  log=%s chars  prev_log=%s chars",
            pod_name,
            len(log) if log else "none",
            len(prev_log) if prev_log else "none",
        )

    logger.debug("[SUMMARIZE] total pods=%d, deployments=%d, nodes=%d, services=%d, pvcs=%d",
                 len(pod_data),
                 len(state.get("raw_deployment_data") or []),
                 len(state.get("raw_node_data") or []),
                 len(state.get("raw_service_data") or []),
                 len(state.get("raw_pvc_data") or []))

    _top_pod = state.get("raw_top_pod_output")
    _top_nodes = state.get("raw_top_nodes_output")
    if _top_pod:
        logger.debug("[SUMMARIZE] including live pod metrics: %s", _top_pod[:80])
    if _top_nodes:
        logger.debug("[SUMMARIZE] including live node metrics: %s", _top_nodes[:120])

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
        top_pod_output=_top_pod,
        top_nodes_output=_top_nodes,
    )

    logger.info("Summarized context: %d tokens", structured.token_count)
    logger.debug("[SUMMARIZE] structured context keys: %s",
                 list(structured.model_dump().keys()))

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

    logger.debug("[RAG] starting retrieval for query=%r", query)

    # Run in executor to avoid blocking (first run downloads ~130MB model)
    loop = asyncio.get_event_loop()
    try:
        rag_context = await asyncio.wait_for(
            loop.run_in_executor(None, rag.retrieve, query),
            timeout=60.0,   # first run can take 30-60s to download + load model
        )
        logger.debug("[RAG] retrieved %d chars of context", len(rag_context) if rag_context else 0)
        if rag_context:
            logger.debug("[RAG] context preview: %s...", rag_context[:120].replace("\n", " "))
    except asyncio.TimeoutError:
        rag_context = None
        logger.warning("RAG retrieval timed out (model may still be loading — will be faster next run)")
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

    logger.debug("[ANALYZE] cluster_reachable=%s, has_structured_context=%s, has_rag_context=%s",
                 cluster_reachable, bool(structured_context), bool(rag_context))
    if isinstance(structured_context, dict):
        logger.debug("[ANALYZE] structured_context keys: %s", list(structured_context.keys()))

    # Build appropriate prompt
    if cluster_reachable and structured_context:
        user_prompt = build_analysis_prompt(structured_context, rag_context)
        logger.debug("[ANALYZE] mode=cluster+rag, prompt=%d chars", len(user_prompt))
    else:
        # RAG-only mode
        rag_context = rag_context or _get_rag().retrieve(query)
        user_prompt = build_rag_only_prompt(query, rag_context)
        logger.debug("[ANALYZE] mode=rag-only, prompt=%d chars", len(user_prompt))

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    logger.info("Calling LLM: provider=%s, model=%s", settings.provider.value, settings.model)
    logger.debug("[ANALYZE] total prompt size (system+user): %d chars",
                 len(SYSTEM_PROMPT) + len(user_prompt))

    # Dump full prompts in debug mode so you can verify what the LLM actually sees
    logger.debug(
        "[LLM-PROMPT] ════════════ SYSTEM PROMPT (%d chars) ════════════\n%s",
        len(SYSTEM_PROMPT), SYSTEM_PROMPT,
    )
    logger.debug(
        "[LLM-PROMPT] ════════════ USER PROMPT / CONTEXT (%d chars) ════════════\n%s",
        len(user_prompt), user_prompt,
    )

    try:
        llm = LLMFactory.create(settings)

        # Run in executor (LangChain sync invoke)
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: invoke_llm_with_retry(llm, messages),
        )

        raw_content = response.content if hasattr(response, "content") else str(response)
        logger.debug("[ANALYZE] LLM response: %d chars", len(raw_content))
        logger.debug(
            "[LLM-RESPONSE] ════════════ RAW LLM RESPONSE (%d chars) ════════════\n%s",
            len(raw_content), raw_content,
        )

        # Parse JSON from response
        diagnosis = _extract_json(raw_content)
        logger.debug("[ANALYZE] JSON parse success=%s", bool(diagnosis))

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
