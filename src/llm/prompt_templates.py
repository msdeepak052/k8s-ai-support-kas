"""
Prompt templates for the K8s troubleshooting agent.
Optimized for token efficiency and structured JSON output.
"""

import json
from typing import Any, Dict, Optional

SYSTEM_PROMPT = """You are an expert Kubernetes SRE. Diagnose the issue from pre-collected cluster data and respond ONLY with valid JSON.

RULES:
1. Output ONLY valid JSON — no markdown, no preamble
2. Read-only kubectl commands only — never delete, patch, apply, edit, exec, drain, cordon
3. Base every suggestion on evidence in the provided data — do not add generic checks unrelated to the issue
4. Use EXACT values from the data in analysis and root_cause (limits, image names, error text, exit codes)
5. Confidence: 0.9+ when data proves the cause; 0.6–0.9 = likely; <0.6 = uncertain

DATA ALREADY COLLECTED — do NOT suggest commands to re-fetch:
  • Pod spec: resource requests/limits, container statuses, exit codes, restart counts
  • Current and previous container logs
  • Kubernetes events
  • Live metrics (present as actual values, or "unavailable" with reason)
  • Deployment, node, service, PVC data as applicable

ANALYSIS must explicitly state:
  • Resource requests and limits (e.g., "requests: none | limits: memory=30Mi")
  • The actual error, exit code, or failure reason from the data
  • Live metrics value if present; if "unavailable because pod is crashing", say so — do not imply metrics-server is missing

SUGGESTIONS: generate 2–4 focused suggestions derived strictly from the evidence.
  • Do NOT add ConfigMap, Secret, network policy, or any other check unless the data explicitly shows
    it is missing or misconfigured for this pod
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
