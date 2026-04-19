"""
Prompt templates for the K8s troubleshooting agent.
Optimized for token efficiency and structured JSON output.
"""

import json
import re
from typing import Any, Dict, List, Optional

SYSTEM_PROMPT = """You are an expert Kubernetes SRE. Diagnose the issue from pre-collected cluster data and respond ONLY with valid JSON.

RULES:
1. Output ONLY valid JSON — no markdown, no preamble
2. Read-only kubectl commands only — never delete, patch, apply, edit, exec, drain, cordon
3. Base every suggestion on evidence in the provided data — do not add generic checks unrelated to the issue
4. Use EXACT values from the data in analysis and root_cause (limits, image names, error text, exit codes)
5. Confidence: 0.9+ when data proves the cause; 0.6–0.9 = likely; <0.6 = uncertain

DATA ALREADY COLLECTED — do NOT suggest commands to re-fetch any of this:
  • pod_summaries[].resource_requests and resource_limits — exact values in the JSON
  • pod_summaries[].owner_references — actual owner (Deployment, StatefulSet, Job, etc.); if empty, pod is standalone
  • pod_summaries[].container_statuses[].command and args — what the container actually runs
    (e.g., args: ["--vm-bytes","100M"] tells you the container intentionally requests 100 MB of memory)
  • pod_summaries[].container_statuses — state, exit_code, restart_count, last_termination_reason
  • pod_summaries[].container_statuses[].last_termination_log_snippet — previous crash log tail;
    null means the container exited before the container runtime flushed its log buffer (common for OOMKill)
  • events[] — already sorted and summarized
  • live_pod_metrics / live_node_metrics — present as actual values or "unavailable" with reason
  • Deployment, node, service, PVC data as applicable

ANALYSIS field — write a concise prose explanation (2-4 sentences) focusing on WHY the issue occurs
and what the resulting behavior is. Do NOT use newlines or bullet points inside the analysis string.
Example: "The container 'memory-hog' runs 'stress --vm-bytes 100M' which allocates 100 MB on every start.
The memory limit is 30Mi, so the OOM killer terminates it instantly before any work completes.
Kubernetes then restarts the container after exponential backoff, repeating the cycle indefinitely."

pod_context field — populate with exact values from the cluster data for structured display:
  "pod":        "oom-pod  (phase: Running, status: CrashLoopBackOff)"
  "container":  "memory-hog  (restarts: 47, last_termination: OOMKilled exit 1)"
  "command":    exact args joined as one string — "stress --vm 1 --vm-bytes 100M --vm-hang 0"
  "requests":   "cpu=10m  memory=10Mi"  (or "none" if not set)
  "limits":     "cpu=100m  memory=30Mi" (or "none" if not set)
  "live_metrics": exact live_pod_metrics value, or "unavailable — pod is crashing"
  "owner":      from owner_references — "Deployment/oom-deploy" or "Standalone pod (no owner)"
  If cluster data is not available, omit pod_context entirely (set to null).

SUGGESTIONS: generate 2–4 focused, evidence-based suggestions.
  • For OOM issues: calculate the recommended new memory limit from the actual memory requested in args
    (e.g., if --vm-bytes 100M → recommend at least 128Mi to provide headroom above 100Mi).
    Use the args value, not an arbitrary multiple of the current limit. State the exact recommended value.
  • Never add generic checks (ConfigMaps, Secrets, network policies, VPA, memory leak review) unless the
    data explicitly shows that resource is missing or misconfigured for this specific pod
  • Every suggestion must include at least one kubectl command — omit the suggestion if you have no command
  • The final suggestion must be a post-fix verification step

additional_checks: non-command guidance only (e.g., "contact app team").

OUTPUT SCHEMA:
{
  "diagnosis": {
    "root_cause": "specific root cause description",
    "confidence": 0.92,
    "affected_resources": ["pod/nginx-xxx", "deployment/nginx"],
    "severity": "critical|high|medium|low",
    "category": "crashloop|imagepull|oom|pending|network|storage|node|config|other"
  },
  "pod_context": {
    "pod": "oom-pod  (phase: Running, status: CrashLoopBackOff)",
    "container": "memory-hog  (restarts: 47, last_termination: OOMKilled exit 1)",
    "command": "stress --vm 1 --vm-bytes 100M --vm-hang 0",
    "requests": "cpu=10m  memory=10Mi",
    "limits": "cpu=100m  memory=30Mi",
    "live_metrics": "unavailable — pod is crashing",
    "owner": "Standalone pod (no owner)"
  },
  "analysis": "detailed technical explanation of what is happening and why",
  "suggestions": [
    {
      "description": "specific action description",
      "commands": ["kubectl get pods -o wide -n default"],
      "priority": "high|medium|low",
      "expected_output": "what to look for in the output"
    }
  ],
  "additional_checks": ["only non-command guidance — e.g. app team escalation, infrastructure ticket"],
  "estimated_fix_time": "quick (< 5 min)|moderate (5-30 min)|involved (30+ min)"
}"""


def build_analysis_prompt(context_json: Dict[str, Any], rag_context: Optional[str] = None) -> str:
    """
    Build the user message for LLM analysis.
    Combines structured cluster context with optional RAG documentation.
    """
    parts = []

    # User query
    query = context_json.get("query", "Analyze the Kubernetes issue")
    parts.append(f"USER QUERY: {query}")
    parts.append("")

    # Cluster context
    parts.append("=== LIVE CLUSTER STATE ===")
    # Remove rag_context from the JSON to avoid duplication
    ctx_for_prompt = {k: v for k, v in context_json.items() if k not in ("rag_context", "query")}
    parts.append(json.dumps(ctx_for_prompt, indent=2, default=str))

    # RAG documentation — framed explicitly so LLM treats it as background, not as a command list
    if rag_context:
        parts.append("")
        parts.append("=== KUBERNETES DOCUMENTATION (background reference only) ===")
        parts.append("NOTE: The 'Diagnostic commands' listed in the docs below were already executed to")
        parts.append("collect the cluster data above. Do NOT copy them into your suggestions.")
        parts.append(rag_context)

    parts.append("")
    parts.append("Analyze the above and respond with the JSON diagnosis schema.")

    return "\n".join(parts)


def build_rag_only_prompt(query: str, rag_context: str) -> str:
    """
    Prompt for when cluster is unreachable — RAG-only mode.
    """
    return f"""USER QUERY: {query}

CLUSTER STATUS: Unreachable (no live data available)

=== KUBERNETES DOCUMENTATION CONTEXT ===
{rag_context}

Provide general guidance based on the documentation context.
Respond with the JSON diagnosis schema. Set confidence to 0.5 or lower since no live data is available.
Set affected_resources to [] since cluster is unreachable."""


def build_resource_summary_prompt(resource_type: str, resource_data: Dict[str, Any]) -> str:
    """Prompt for summarizing a specific resource."""
    return f"""Summarize the key status and any issues for this Kubernetes {resource_type}:

{json.dumps(resource_data, indent=2, default=str)}

Focus on: phase/status, error conditions, resource constraints, and notable configuration."""


def format_diagnosis_as_table(diagnosis: Dict[str, Any]) -> str:
    """Format LLM diagnosis response as human-readable CLI output."""
    W = 72  # total line width

    def _wrap(text: str, indent: str = "  ") -> list:
        """Render text preserving structure:
          - \\n\\n between sections → blank line in output
          - \\n within a section   → new indented line (word-wrapped if long)
        """
        out = []
        sections = text.strip().split("\n\n")
        for si, section in enumerate(sections):
            if si > 0:
                out.append("")          # blank line between sections
            for line in section.split("\n"):
                line = line.strip()
                if not line:
                    out.append("")
                    continue
                # Word-wrap long lines
                words = line.split()
                cur = indent
                for word in words:
                    candidate = (indent + word) if cur == indent else (cur + " " + word)
                    if len(candidate) > W:
                        out.append(cur.rstrip())
                        cur = indent + word
                    else:
                        cur = candidate
                if cur.strip():
                    out.append(cur.rstrip())
        return out

    HEAVY = "═" * W
    RULE  = "  " + "─" * (W - 2)

    lines = []
    lines.append(HEAVY)
    lines.append("  K8S-AI-SUPPORT  DIAGNOSIS")
    lines.append(HEAVY)

    diag       = diagnosis.get("diagnosis", {})
    severity   = diag.get("severity", "unknown").upper()
    category   = diag.get("category", "unknown")
    confidence = f"{diag.get('confidence', 0.0):.0%}"
    affected   = "  ".join(diag.get("affected_resources", []))

    lines.append(f"  Severity  : {severity:<16} Category   : {category}")
    lines.append(f"  Confidence: {confidence:<16} Affected   : {affected or '—'}")
    lines.append("")

    # ── Root Cause ──
    lines.append("  ROOT CAUSE")
    lines.append(RULE)
    lines.extend(_wrap(diag.get("root_cause", "Unknown")))
    lines.append("")

    # ── Analysis (pod_context + prose) ──
    pod_ctx   = diagnosis.get("pod_context") or {}
    analysis  = diagnosis.get("analysis", "").strip()
    if pod_ctx or analysis:
        lines.append("  ANALYSIS")
        lines.append(RULE)

        if pod_ctx:
            _LABELS = {
                "pod":          "Pod         ",
                "container":    "Container   ",
                "command":      "Command     ",
                "requests":     "Requests    ",
                "limits":       "Limits      ",
                "live_metrics": "Live Metrics",
                "owner":        "Owner       ",
            }
            for key, label in _LABELS.items():
                val = pod_ctx.get(key)
                if val:
                    lines.extend(_wrap(f"{label}: {val}", indent="  "))
            lines.append("")

        if analysis:
            lines.extend(_wrap(analysis))
        lines.append("")

    # ── Suggested Actions ──
    suggestions = diagnosis.get("suggestions", [])
    if suggestions:
        lines.append("  SUGGESTED ACTIONS")
        lines.append(RULE)
        for i, sugg in enumerate(suggestions, 1):
            priority = sugg.get("priority", "medium").upper()
            desc     = sugg.get("description", "")
            cmds     = sugg.get("commands", [])
            expected = (sugg.get("expected_output") or "").strip()

            # Suggestion header — wrap long descriptions
            tag    = f"  [{priority}] {i}. "
            cont   = " " * len(tag)
            desc_lines = _wrap(desc, indent=cont)
            if desc_lines:
                lines.append(tag + desc_lines[0].lstrip())
                lines.extend(desc_lines[1:])
            else:
                lines.append(tag)

            # Commands — always on their own line, clearly marked
            for cmd in cmds:
                lines.append(f"       $ {cmd}")

            # Expected output — skip generic/empty values
            if expected and expected.lower() not in ("n/a", "na", "none", ""):
                for exp_line in _wrap(expected, indent="       ↳ "):
                    lines.append(exp_line)

            lines.append("")

    # ── Additional Checks ──
    additional = diagnosis.get("additional_checks", [])
    if additional:
        lines.append("  ADDITIONAL CHECKS")
        lines.append(RULE)
        for check in additional:
            lines.extend(_wrap(f"• {check}", indent="  "))
        lines.append("")

    # ── ETA ──
    eta = diagnosis.get("estimated_fix_time")
    if eta:
        lines.append(f"  Estimated Fix Time : {eta}")
        lines.append("")

    lines.append(HEAVY)
    return "\n".join(lines)


def format_diagnosis_as_yaml(diagnosis: Dict[str, Any]) -> str:
    """Format diagnosis as YAML."""
    import yaml
    return yaml.dump(diagnosis, default_flow_style=False, allow_unicode=True)
