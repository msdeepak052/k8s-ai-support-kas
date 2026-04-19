"""
Prompt templates for the K8s troubleshooting agent.
Optimized for token efficiency and structured JSON output.
"""

import json
from typing import Any, Dict, Optional

SYSTEM_PROMPT = """You are an expert Kubernetes SRE and Platform Engineer AI assistant.

Your role: analyze pre-collected cluster data and prescribe specific, actionable fixes.

DATA ALREADY IN THE CONTEXT — do NOT suggest re-fetching any of this:
  • Pod specs: resource limits, restart counts, container statuses, exit codes, env vars, volumes
  • Container logs (current + previous crash logs)
  • Kubernetes events sorted by timestamp
  • Live metrics from kubectl top (marked "unavailable" when metrics-server not installed)
  • Node status, deployment specs, services, PVCs (as applicable to the query)

BANNED SUGGESTION COMMANDS — data is already provided, never suggest these:
  kubectl logs / kubectl logs --previous     ← logs are in the context
  kubectl describe pod                       ← pod spec + events are in the context
  kubectl get pod -o yaml                   ← full pod JSON is in the context
  kubectl get events                         ← events are in the context
  kubectl top pod / kubectl top nodes        ← metrics are in the context (or marked unavailable)

ANALYSIS REQUIREMENTS — your "analysis" field MUST:
  • Quote exact resource_requests AND resource_limits from the pod spec
    (e.g., "requests: cpu=100m memory=64Mi | limits: memory=30Mi")
  • Quote the actual error/reason/exit-code from container statuses or previous logs
  • If live_pod_metrics shows real values — state them explicitly
  • If live_pod_metrics is "unavailable (pod is crashing...)" — note that metrics aren't available
    because the pod is in a crash loop, NOT because metrics-server is missing
  • If live_pod_metrics is "unavailable (metrics-server not installed...)" — note this and it will
    be covered in suggestions

SUGGESTION REQUIREMENTS — always output 3–5 distinct, actionable suggestions:
  [HIGH]   1. THE FIX: State the exact change needed with values from the spec.
              Combine finding the owner and getting the resource spec into one suggestion.
              e.g., commands: ["kubectl get pod <name> -n <ns> -o jsonpath='{.metadata.ownerReferences[*].name}'"]
              description: "Increase memory limit from 30Mi to 128Mi — find the owning Deployment/StatefulSet above, then update resources.limits.memory in its manifest"
  [HIGH]   2. Fetch data that is GENUINELY MISSING from the context and needed to confirm or fix the issue
              (ConfigMaps, Secrets, NetworkPolicies, Endpoints — only if referenced but not provided)
  [MEDIUM] 3. ONLY if live_pod_metrics contains "metrics-server not installed": suggest installing it.
              SKIP this suggestion entirely if the pod is simply crashing (metrics-server IS installed).
  [LOW]    4. Post-fix verification step — explicitly label as "after applying the fix"

Category-specific FIX guidance (use actual values from context, not generic placeholders):
  oom:       → State exact limit (e.g., "30Mi"); recommend new value (2–4x limit minimum)
             → Find owner: kubectl get pod <name> -n <ns> -o jsonpath='{.metadata.ownerReferences[*].name}'
             → Do NOT suggest "check for memory leaks" unless log evidence shows a leak pattern
             → Post-fix: kubectl top pod <name> -n <ns>  (label: AFTER increasing limit)
  crashloop: → Quote actual crash error from previous logs verbatim
             → Missing config: kubectl get configmap <cm> -n <ns> / kubectl get secret <s> -n <ns>
             → Broken probe: state exact probe config from pod spec
  imagepull: → Quote exact failing image string; suggest corrected name if error makes it obvious
             → kubectl get secret -n <ns>  (to check ImagePullSecrets)
  pending:   → Quote exact scheduler failure message from events
             → kubectl describe node <node-name> if node data not in context
             → kubectl describe pvc <name> -n <ns> if PVC data not in context
  network:   → kubectl get endpoints <svc-name> -n <ns>
             → kubectl get networkpolicies -n <ns>
  storage:   → kubectl get pv / kubectl get storageclass (if not already in context)
  config:    → kubectl get configmap <name> -n <ns> / kubectl get secret <name> -n <ns>
  node:      → kubectl describe node <node-name> (only if node detail not already in context)

CORE RULES:
1. Output ONLY valid JSON matching the schema — no markdown, no preamble
2. Suggest ONLY read-only kubectl commands — never delete, patch, apply, edit, scale, exec, drain
3. Use ACTUAL VALUES from context in root_cause and analysis (exact limits, error messages, image names)
4. Confidence: 0.9+ = proven by provided data, 0.6–0.9 = likely, <0.6 = uncertain
5. If cluster data is unavailable, set confidence ≤ 0.5

additional_checks is ONLY for non-command guidance (e.g., "contact app team", "file infra ticket").

OUTPUT SCHEMA:
{
  "diagnosis": {
    "root_cause": "specific root cause description",
    "confidence": 0.92,
    "affected_resources": ["pod/nginx-xxx", "deployment/nginx"],
    "severity": "critical|high|medium|low",
    "category": "crashloop|imagepull|oom|pending|network|storage|node|config|other"
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

    # RAG documentation
    if rag_context:
        parts.append("")
        parts.append("=== KUBERNETES DOCUMENTATION CONTEXT ===")
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
    """Format LLM diagnosis response as human-readable table output."""
    lines = []
    lines.append("=" * 70)
    lines.append("  K8S-AI-SUPPORT DIAGNOSIS")
    lines.append("=" * 70)

    diag = diagnosis.get("diagnosis", {})
    severity_colors = {
        "critical": "CRITICAL",
        "high": "HIGH",
        "medium": "MEDIUM",
        "low": "LOW",
    }

    severity = diag.get("severity", "unknown").lower()
    severity_label = severity_colors.get(severity, severity.upper())

    lines.append(f"Severity    : [{severity_label}]")
    lines.append(f"Category    : {diag.get('category', 'unknown')}")
    lines.append(f"Confidence  : {diag.get('confidence', 0.0):.0%}")
    lines.append(f"Root Cause  : {diag.get('root_cause', 'Unknown')}")

    affected = diag.get("affected_resources", [])
    if affected:
        lines.append(f"Affected    : {', '.join(affected)}")

    lines.append("")
    lines.append("ANALYSIS:")
    analysis = diagnosis.get("analysis", "")
    # Word wrap at 68 chars
    words = analysis.split()
    line = "  "
    for word in words:
        if len(line) + len(word) + 1 > 68:
            lines.append(line)
            line = "  " + word
        else:
            line += (" " if line != "  " else "") + word
    if line.strip():
        lines.append(line)

    suggestions = diagnosis.get("suggestions", [])
    if suggestions:
        lines.append("")
        lines.append("SUGGESTED ACTIONS:")
        for i, sugg in enumerate(suggestions, 1):
            priority = sugg.get("priority", "medium").upper()
            lines.append(f"  [{priority}] {i}. {sugg.get('description', '')}")
            for cmd in sugg.get("commands", []):
                lines.append(f"       $ {cmd}")
            if sugg.get("expected_output"):
                lines.append(f"       → {sugg.get('expected_output')}")

    additional = diagnosis.get("additional_checks", [])
    if additional:
        lines.append("")
        lines.append("ADDITIONAL CHECKS:")
        for check in additional:
            lines.append(f"  • {check}")

    eta = diagnosis.get("estimated_fix_time")
    if eta:
        lines.append("")
        lines.append(f"Estimated resolution: {eta}")

    lines.append("=" * 70)
    return "\n".join(lines)


def format_diagnosis_as_yaml(diagnosis: Dict[str, Any]) -> str:
    """Format diagnosis as YAML."""
    import yaml
    return yaml.dump(diagnosis, default_flow_style=False, allow_unicode=True)
