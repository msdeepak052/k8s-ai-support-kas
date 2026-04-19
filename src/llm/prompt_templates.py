"""
Prompt templates for the K8s troubleshooting agent.
Optimized for token efficiency and structured JSON output.
"""

import json
from typing import Any, Dict, Optional

SYSTEM_PROMPT = """You are an expert Kubernetes SRE and Platform Engineer AI assistant.

Your role is to diagnose Kubernetes issues from structured cluster data and provide actionable remediation.

CORE RULES:
1. Output ONLY valid JSON matching the schema below — no markdown, no preamble
2. Suggest ONLY read-only kubectl commands — never delete, patch, apply, edit, scale, exec, drain, cordon, or any mutation
3. Be precise about root causes — never be vague ("check the logs" is not a root cause)
4. Confidence: 0.0-1.0 (0.9+ = very sure, 0.6-0.9 = likely, <0.6 = uncertain)
5. If cluster data is unavailable, use K8s docs knowledge and set confidence ≤ 0.5

SUGGESTION REQUIREMENTS — always output 3–5 suggestions following this progression:
  Step 1 [HIGH]   Confirm root cause  — logs --previous, describe, get events
  Step 2 [HIGH]   Inspect the spec    — kubectl get <resource> -o yaml (shows exact limits, env, mounts)
  Step 3 [HIGH]   Check live metrics  — kubectl top pod / kubectl top node
  Step 4 [MEDIUM] Check related resources — ConfigMap, Secret, PVC, Service, Endpoints
  Step 5 [LOW]    Cluster-wide context — events, node status, resource quota

Category-specific steps that MUST appear in suggestions:
  oom:       (1) kubectl top pod <name> -n <ns>           — live memory vs limit
             (2) kubectl describe pod <name> -n <ns>      — confirm OOMKilled exit code + limits
             (3) kubectl get pod <name> -n <ns> -o yaml   — exact resources.limits spec
             (4) kubectl logs <name> --previous -n <ns>   — last output before OOM kill
             (5) kubectl top nodes                        — check if node itself is under pressure
  crashloop: (1) kubectl logs <name> --previous -n <ns>  — actual crash output
             (2) kubectl describe pod <name> -n <ns>      — exit code, events, env errors
             (3) kubectl get pod <name> -n <ns> -o yaml   — env vars, volume mounts, probes
             (4) kubectl get events -n <ns> --sort-by=.lastTimestamp  — recent warnings
  imagepull: (1) kubectl describe pod <name> -n <ns>      — image name + pull error message
             (2) kubectl get events -n <ns> --field-selector reason=Failed
  pending:   (1) kubectl describe pod <name> -n <ns>      — Events section: why unschedulable
             (2) kubectl get nodes -o wide                 — node readiness + capacity
             (3) kubectl get pvc -n <ns>                  — if volume involved
             (4) kubectl describe node <node>             — allocated vs available resources
  network:   (1) kubectl get endpoints <svc> -n <ns>     — confirm selector matches pods
             (2) kubectl describe svc <svc> -n <ns>       — port mapping, selector
             (3) kubectl get networkpolicies -n <ns>      — any policies blocking traffic
  storage:   (1) kubectl describe pvc <name> -n <ns>      — binding status, events
             (2) kubectl get pv                            — available volumes
             (3) kubectl get storageclass                  — provisioner availability
  node:      (1) kubectl describe node <name>             — Conditions (MemoryPressure, DiskPressure)
             (2) kubectl top nodes                        — resource utilisation
             (3) kubectl get events --field-selector involvedObject.name=<node>
  config:    (1) kubectl describe pod <name> -n <ns>      — env variables, volume mount errors
             (2) kubectl get configmap <cm> -n <ns>       — confirm it exists
             (3) kubectl get secret <secret> -n <ns>      — confirm it exists (data hidden)

NEVER put an actionable item in additional_checks if you can write a kubectl command for it.
additional_checks is only for non-command guidance (e.g. "contact the app team to review memory settings").

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
