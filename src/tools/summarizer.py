"""
Token-efficient summarizer: converts raw kubectl output → structured context.
Target: 95% token reduction (15KB raw → ~800 bytes structured).
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Optional tiktoken for accurate token counting
try:
    import tiktoken
    _ENCODER = tiktoken.get_encoding("cl100k_base")
    def count_tokens(text: str) -> int:
        return len(_ENCODER.encode(text))
except ImportError:
    def count_tokens(text: str) -> int:
        # Rough estimate: 1 token ≈ 4 chars
        return len(text) // 4


# ─────────────────────────── Pydantic Models ────────────────────────────────

class ContainerStatus(BaseModel):
    name: str
    ready: bool = False
    restart_count: int = 0
    state: str = "unknown"
    reason: Optional[str] = None
    exit_code: Optional[int] = None
    last_termination_reason: Optional[str] = None
    last_termination_exit_code: Optional[int] = None
    last_termination_log_snippet: Optional[str] = None
    image: Optional[str] = None


class PodSummary(BaseModel):
    resource_type: str = "Pod"
    name: str
    namespace: str
    phase: str
    node: Optional[str] = None
    pod_ip: Optional[str] = None
    host_ip: Optional[str] = None
    conditions: List[Dict[str, str]] = Field(default_factory=list)
    container_statuses: List[ContainerStatus] = Field(default_factory=list)
    events: List[Dict[str, Any]] = Field(default_factory=list)
    resource_requests: Optional[Dict[str, Any]] = None
    resource_limits: Optional[Dict[str, Any]] = None
    volumes: List[str] = Field(default_factory=list)
    creation_timestamp: Optional[str] = None
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations_count: int = 0


class DeploymentSummary(BaseModel):
    resource_type: str = "Deployment"
    name: str
    namespace: str
    replicas_desired: int = 0
    replicas_ready: int = 0
    replicas_available: int = 0
    replicas_updated: int = 0
    strategy: Optional[str] = None
    conditions: List[Dict[str, str]] = Field(default_factory=list)
    selector: Dict[str, str] = Field(default_factory=dict)
    image: Optional[str] = None
    creation_timestamp: Optional[str] = None


class NodeSummary(BaseModel):
    resource_type: str = "Node"
    name: str
    status: str
    roles: List[str] = Field(default_factory=list)
    os: Optional[str] = None
    kernel: Optional[str] = None
    container_runtime: Optional[str] = None
    cpu_capacity: Optional[str] = None
    memory_capacity: Optional[str] = None
    cpu_allocatable: Optional[str] = None
    memory_allocatable: Optional[str] = None
    conditions: List[Dict[str, str]] = Field(default_factory=list)
    taints: List[str] = Field(default_factory=list)
    kubelet_version: Optional[str] = None


class EventSummary(BaseModel):
    type: str
    reason: str
    message: str
    count: int = 1
    last_timestamp: Optional[str] = None
    involved_object: str = ""


class ServiceSummary(BaseModel):
    resource_type: str = "Service"
    name: str
    namespace: str
    type: str
    cluster_ip: Optional[str] = None
    external_ip: Optional[str] = None
    ports: List[str] = Field(default_factory=list)
    selector: Dict[str, str] = Field(default_factory=dict)


class PVCSummary(BaseModel):
    resource_type: str = "PersistentVolumeClaim"
    name: str
    namespace: str
    phase: str
    storage_class: Optional[str] = None
    capacity: Optional[str] = None
    access_modes: List[str] = Field(default_factory=list)
    volume_name: Optional[str] = None


class StructuredContext(BaseModel):
    """Top-level container for all summarized resource data sent to the LLM."""
    query: str
    namespace: str
    cluster_reachable: bool = True
    pod_summaries: List[PodSummary] = Field(default_factory=list)
    deployment_summaries: List[DeploymentSummary] = Field(default_factory=list)
    node_summaries: List[NodeSummary] = Field(default_factory=list)
    service_summaries: List[ServiceSummary] = Field(default_factory=list)
    pvc_summaries: List[PVCSummary] = Field(default_factory=list)
    events: List[EventSummary] = Field(default_factory=list)
    raw_log_snippet: Optional[str] = None
    # Live resource metrics from "kubectl top" — actual current usage vs limits
    live_pod_metrics: Optional[str] = None    # e.g. "cpu=1m, memory=45Mi"
    live_node_metrics: Optional[str] = None   # full kubectl top nodes output
    rag_context: Optional[str] = None
    token_count: int = 0
    warnings: List[str] = Field(default_factory=list)

    def estimate_tokens(self) -> int:
        return count_tokens(self.model_dump_json())


# ─────────────────────────── Summarizer ─────────────────────────────────────

class ResourceSummarizer:
    """
    Converts raw kubectl JSON/text output into compact structured summaries.
    """

    def __init__(self, max_log_lines: int = 10, token_budget: int = 8000):
        self.max_log_lines = max_log_lines
        self.token_budget = token_budget

    def summarize_pod(self, pod_json: Dict[str, Any], log_text: Optional[str] = None, prev_log_text: Optional[str] = None) -> PodSummary:
        """Extract essential fields from full pod JSON."""
        meta = pod_json.get("metadata", {})
        spec = pod_json.get("spec", {})
        status = pod_json.get("status", {})

        # Container statuses
        container_statuses = []
        for cs in status.get("containerStatuses", []):
            state = cs.get("state", {})
            state_str = "unknown"
            reason = None
            exit_code = None

            if "running" in state:
                state_str = "running"
            elif "waiting" in state:
                state_str = "waiting"
                reason = state["waiting"].get("reason")
            elif "terminated" in state:
                state_str = "terminated"
                reason = state["terminated"].get("reason")
                exit_code = state["terminated"].get("exitCode")

            # Last termination
            lt = cs.get("lastState", {}).get("terminated", {})
            lt_reason = lt.get("reason")
            lt_exit = lt.get("exitCode")

            # Log snippet from previous crash
            log_snippet = None
            if prev_log_text:
                lines = prev_log_text.strip().split("\n")
                log_snippet = "\n".join(lines[-self.max_log_lines:])
            elif log_text and state_str in ("waiting", "terminated"):
                lines = log_text.strip().split("\n")
                log_snippet = "\n".join(lines[-self.max_log_lines:])

            # Resource requests/limits from spec
            container_spec = next(
                (c for c in spec.get("containers", []) if c.get("name") == cs.get("name")), {}
            )

            container_statuses.append(ContainerStatus(
                name=cs.get("name", "unknown"),
                ready=cs.get("ready", False),
                restart_count=cs.get("restartCount", 0),
                state=state_str,
                reason=reason,
                exit_code=exit_code,
                last_termination_reason=lt_reason,
                last_termination_exit_code=lt_exit,
                last_termination_log_snippet=log_snippet,
                image=cs.get("image"),
            ))

        # Conditions (only non-True or problematic)
        conditions = []
        for cond in status.get("conditions", []):
            if cond.get("status") != "True" or cond.get("type") in ("Ready",):
                conditions.append({
                    "type": cond.get("type", ""),
                    "status": cond.get("status", ""),
                    "reason": cond.get("reason", ""),
                    "message": cond.get("message", "")[:200],  # truncate
                })

        # Resource requests/limits (aggregate across containers)
        total_requests: Dict[str, str] = {}
        total_limits: Dict[str, str] = {}
        for container in spec.get("containers", []):
            res = container.get("resources", {})
            total_requests.update(res.get("requests", {}))
            total_limits.update(res.get("limits", {}))

        # Volume names (PVC references only)
        pvc_volumes = [
            v.get("persistentVolumeClaim", {}).get("claimName", "")
            for v in spec.get("volumes", [])
            if "persistentVolumeClaim" in v
        ]

        return PodSummary(
            name=meta.get("name", "unknown"),
            namespace=meta.get("namespace", "default"),
            phase=status.get("phase", "Unknown"),
            node=spec.get("nodeName"),
            pod_ip=status.get("podIP"),
            host_ip=status.get("hostIP"),
            conditions=conditions,
            container_statuses=container_statuses,
            resource_requests=total_requests or None,
            resource_limits=total_limits or None,
            volumes=pvc_volumes,
            creation_timestamp=meta.get("creationTimestamp"),
            labels=meta.get("labels", {}),
            annotations_count=len(meta.get("annotations", {})),
        )

    def summarize_deployment(self, dep_json: Dict[str, Any]) -> DeploymentSummary:
        """Extract essential fields from full deployment JSON."""
        meta = dep_json.get("metadata", {})
        spec = dep_json.get("spec", {})
        status = dep_json.get("status", {})

        # Image from first container
        containers = spec.get("template", {}).get("spec", {}).get("containers", [])
        image = containers[0].get("image") if containers else None

        conditions = []
        for cond in status.get("conditions", []):
            if cond.get("status") != "True":
                conditions.append({
                    "type": cond.get("type", ""),
                    "status": cond.get("status", ""),
                    "reason": cond.get("reason", ""),
                    "message": cond.get("message", "")[:200],
                })

        return DeploymentSummary(
            name=meta.get("name", "unknown"),
            namespace=meta.get("namespace", "default"),
            replicas_desired=spec.get("replicas", 0),
            replicas_ready=status.get("readyReplicas", 0),
            replicas_available=status.get("availableReplicas", 0),
            replicas_updated=status.get("updatedReplicas", 0),
            strategy=spec.get("strategy", {}).get("type"),
            conditions=conditions,
            selector=spec.get("selector", {}).get("matchLabels", {}),
            image=image,
            creation_timestamp=meta.get("creationTimestamp"),
        )

    def summarize_node(self, node_json: Dict[str, Any]) -> NodeSummary:
        """Extract essential fields from node JSON."""
        meta = node_json.get("metadata", {})
        status = node_json.get("status", {})
        spec = node_json.get("spec", {})

        # Roles from labels
        labels = meta.get("labels", {})
        roles = [
            k.split("/")[-1]
            for k in labels
            if k.startswith("node-role.kubernetes.io/")
        ]

        # Node status (Ready/NotReady)
        conditions = status.get("conditions", [])
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), {})
        node_status = "Ready" if ready_cond.get("status") == "True" else "NotReady"

        # Only problematic conditions
        bad_conditions = []
        for cond in conditions:
            if cond.get("status") != "False" and cond.get("type") != "Ready":
                bad_conditions.append({
                    "type": cond.get("type", ""),
                    "status": cond.get("status", ""),
                    "reason": cond.get("reason", ""),
                })

        info = status.get("nodeInfo", {})
        capacity = status.get("capacity", {})
        allocatable = status.get("allocatable", {})
        taints = [f"{t.get('key')}={t.get('value', '')}:{t.get('effect')}" for t in spec.get("taints", [])]

        return NodeSummary(
            name=meta.get("name", "unknown"),
            status=node_status,
            roles=roles or ["worker"],
            os=info.get("operatingSystem"),
            kernel=info.get("kernelVersion"),
            container_runtime=info.get("containerRuntimeVersion"),
            cpu_capacity=capacity.get("cpu"),
            memory_capacity=capacity.get("memory"),
            cpu_allocatable=allocatable.get("cpu"),
            memory_allocatable=allocatable.get("memory"),
            conditions=bad_conditions,
            taints=taints,
            kubelet_version=info.get("kubeletVersion"),
        )

    def summarize_events(self, events_json: Dict[str, Any], max_events: int = 10) -> List[EventSummary]:
        """Extract warning/error events, sorted by recency."""
        items = events_json.get("items", [])

        summaries = []
        for evt in items:
            evt_type = evt.get("type", "Normal")
            reason = evt.get("reason", "")
            message = evt.get("message", "")[:300]
            count = evt.get("count", 1)
            last_ts = evt.get("lastTimestamp") or evt.get("eventTime")
            inv_obj = evt.get("involvedObject", {})
            obj_ref = f"{inv_obj.get('kind', '')}/{inv_obj.get('name', '')}"

            summaries.append(EventSummary(
                type=evt_type,
                reason=reason,
                message=message,
                count=count,
                last_timestamp=last_ts,
                involved_object=obj_ref,
            ))

        # Sort by type (Warning first) then by count
        summaries.sort(key=lambda e: (0 if e.type == "Warning" else 1, -e.count))
        return summaries[:max_events]

    def summarize_service(self, svc_json: Dict[str, Any]) -> ServiceSummary:
        meta = svc_json.get("metadata", {})
        spec = svc_json.get("spec", {})
        status = svc_json.get("status", {})

        ports = [
            f"{p.get('port')}/{p.get('protocol', 'TCP')}" + (f"→{p.get('nodePort')}" if p.get("nodePort") else "")
            for p in spec.get("ports", [])
        ]
        ext_ips = status.get("loadBalancer", {}).get("ingress", [])
        ext_ip = ext_ips[0].get("ip") or ext_ips[0].get("hostname") if ext_ips else None

        return ServiceSummary(
            name=meta.get("name", "unknown"),
            namespace=meta.get("namespace", "default"),
            type=spec.get("type", "ClusterIP"),
            cluster_ip=spec.get("clusterIP"),
            external_ip=ext_ip,
            ports=ports,
            selector=spec.get("selector", {}),
        )

    def summarize_pvc(self, pvc_json: Dict[str, Any]) -> PVCSummary:
        meta = pvc_json.get("metadata", {})
        spec = pvc_json.get("spec", {})
        status = pvc_json.get("status", {})

        return PVCSummary(
            name=meta.get("name", "unknown"),
            namespace=meta.get("namespace", "default"),
            phase=status.get("phase", "Unknown"),
            storage_class=spec.get("storageClassName"),
            capacity=status.get("capacity", {}).get("storage"),
            access_modes=spec.get("accessModes", []),
            volume_name=spec.get("volumeName"),
        )

    def truncate_logs(self, log_text: str) -> str:
        """Keep only the last N lines of logs."""
        if not log_text:
            return ""
        lines = log_text.strip().split("\n")
        if len(lines) > self.max_log_lines:
            lines = lines[-self.max_log_lines:]
        return "\n".join(lines)

    def build_context(
        self,
        query: str,
        namespace: str,
        pod_data: Optional[List[tuple]] = None,  # [(pod_json, log, prev_log), ...]
        deployment_data: Optional[List[Dict]] = None,
        node_data: Optional[List[Dict]] = None,
        service_data: Optional[List[Dict]] = None,
        pvc_data: Optional[List[Dict]] = None,
        events_json: Optional[Dict] = None,
        rag_context: Optional[str] = None,
        cluster_reachable: bool = True,
        warnings: Optional[List[str]] = None,
        top_pod_output: Optional[str] = None,
        top_nodes_output: Optional[str] = None,
    ) -> StructuredContext:
        """Build the complete structured context for the LLM."""

        ctx = StructuredContext(
            query=query,
            namespace=namespace,
            cluster_reachable=cluster_reachable,
            rag_context=rag_context,
            warnings=warnings or [],
        )

        # Parse "kubectl top pod <name>" output into a compact metrics string.
        # Raw format:
        #   NAME      CPU(cores)   MEMORY(bytes)
        #   oom-pod   1m           45Mi
        # Becomes: "cpu=1m  memory=45Mi"
        # When the command was attempted but failed: "unavailable (metrics-server not installed)"
        if top_pod_output:
            _sentinel = top_pod_output.strip()
            if _sentinel == "pod_crashing_no_metrics":
                ctx.live_pod_metrics = "unavailable (pod is crashing — metrics are only collected for running pods)"
            elif _sentinel == "no_metrics_server":
                ctx.live_pod_metrics = "unavailable (metrics-server not installed in this cluster)"
            else:
                lines = [l for l in top_pod_output.strip().splitlines() if l.strip()]
                if len(lines) >= 2:
                    parts = lines[1].split()   # [name, cpu, memory]
                    if len(parts) >= 3:
                        ctx.live_pod_metrics = f"cpu={parts[1]}  memory={parts[2]}"
                    else:
                        ctx.live_pod_metrics = lines[1].strip()
                elif lines:
                    ctx.live_pod_metrics = lines[0].strip()
            logger.debug("live_pod_metrics: %s", ctx.live_pod_metrics)

        # Include full top-nodes output (compact table, useful for node-pressure context)
        if top_nodes_output:
            if top_nodes_output.strip() == "no_metrics_server":
                ctx.live_node_metrics = "unavailable (metrics-server not installed)"
            else:
                ctx.live_node_metrics = top_nodes_output.strip()[:400]

        if pod_data:
            for item in pod_data:
                if isinstance(item, tuple):
                    pod_json, log_text, prev_log = item[0], item[1] if len(item) > 1 else None, item[2] if len(item) > 2 else None
                else:
                    pod_json, log_text, prev_log = item, None, None
                ctx.pod_summaries.append(self.summarize_pod(pod_json, log_text, prev_log))

        if deployment_data:
            ctx.deployment_summaries = [self.summarize_deployment(d) for d in deployment_data]

        if node_data:
            ctx.node_summaries = [self.summarize_node(n) for n in node_data]

        if service_data:
            ctx.service_summaries = [self.summarize_service(s) for s in service_data]

        if pvc_data:
            ctx.pvc_summaries = [self.summarize_pvc(p) for p in pvc_data]

        if events_json:
            ctx.events = self.summarize_events(events_json)

        # Token count
        ctx.token_count = ctx.estimate_tokens()

        # Aggressive trim if over budget
        if ctx.token_count > self.token_budget:
            logger.warning(
                "Context exceeds token budget (%d > %d), trimming...",
                ctx.token_count, self.token_budget
            )
            ctx = self._trim_context(ctx)

        return ctx

    def _trim_context(self, ctx: StructuredContext) -> StructuredContext:
        """Aggressively trim context to fit token budget."""
        # Drop RAG context first
        if ctx.rag_context and ctx.estimate_tokens() > self.token_budget:
            ctx.rag_context = ctx.rag_context[:500] if ctx.rag_context else None

        # Trim log snippets
        for pod in ctx.pod_summaries:
            for cs in pod.container_statuses:
                if cs.last_termination_log_snippet:
                    lines = cs.last_termination_log_snippet.split("\n")
                    cs.last_termination_log_snippet = "\n".join(lines[-5:])

        # Trim events
        if len(ctx.events) > 5:
            ctx.events = ctx.events[:5]

        # Trim node summaries
        if len(ctx.node_summaries) > 3:
            ctx.node_summaries = ctx.node_summaries[:3]

        ctx.token_count = ctx.estimate_tokens()
        return ctx
