"""
Blocklist of forbidden kubectl commands.
Hard-enforced safety layer — the agent can NEVER mutate cluster state.
"""

import re
from typing import List

# Regex patterns for forbidden operations
BLOCKLIST_PATTERNS: List[re.Pattern] = [
    re.compile(r"\bdelete\b", re.IGNORECASE),
    re.compile(r"\bpatch\b", re.IGNORECASE),
    re.compile(r"\bapply\b", re.IGNORECASE),
    re.compile(r"\bedit\b", re.IGNORECASE),
    re.compile(r"\bscale\b", re.IGNORECASE),
    re.compile(r"\bexec\b", re.IGNORECASE),
    re.compile(r"\bcp\b", re.IGNORECASE),
    re.compile(r"\brollout\s+(undo|restart|pause|resume)\b", re.IGNORECASE),
    re.compile(r"\bdrain\b", re.IGNORECASE),
    re.compile(r"\bcordon\b", re.IGNORECASE),
    re.compile(r"\buncordon\b", re.IGNORECASE),
    re.compile(r"\btaint\b", re.IGNORECASE),
    re.compile(r"\blabel\b", re.IGNORECASE),
    re.compile(r"\bannotate\b", re.IGNORECASE),
    re.compile(r"\breplace\b", re.IGNORECASE),
    re.compile(r"\bcreate\b", re.IGNORECASE),
    re.compile(r"\brun\b", re.IGNORECASE),
    re.compile(r"\bexpose\b", re.IGNORECASE),
    re.compile(r"\bset\b", re.IGNORECASE),
    re.compile(r"\bautoscale\b", re.IGNORECASE),
    re.compile(r"\brollout\b", re.IGNORECASE),
    re.compile(r"\bport-forward\b", re.IGNORECASE),
    re.compile(r"\bproxy\b", re.IGNORECASE),
    re.compile(r"\battach\b", re.IGNORECASE),
    # Shell injection guards
    re.compile(r"[;&|`$]"),
    re.compile(r"\.\./"),
    re.compile(r"--server-side"),
    re.compile(r"--force"),
    re.compile(r"--grace-period"),
]

# Allowed kubectl subcommands (allowlist approach)
ALLOWED_SUBCOMMANDS = {
    "get",
    "describe",
    "logs",
    "top",
    "version",
    "cluster-info",
    "api-resources",
    "api-versions",
    "explain",
    "config",
    "rollout status",  # read-only rollout check
}


def is_command_safe(command: str) -> tuple[bool, str]:
    """
    Validate that a kubectl command is safe (read-only).

    Returns:
        (is_safe: bool, reason: str)
    """
    # Normalize whitespace
    cmd = " ".join(command.split())

    # Check blocklist patterns
    for pattern in BLOCKLIST_PATTERNS:
        match = pattern.search(cmd)
        if match:
            return False, f"Forbidden pattern detected: '{match.group()}' in command: {cmd}"

    # Extract subcommand (first word after 'kubectl')
    parts = cmd.split()
    if not parts:
        return False, "Empty command"

    # Strip leading 'kubectl' if present
    if parts[0].lower() == "kubectl":
        parts = parts[1:]

    if not parts:
        return False, "No subcommand provided"

    subcommand = parts[0].lower()
    if subcommand not in ALLOWED_SUBCOMMANDS:
        # Check compound subcommands (e.g., "rollout status")
        compound = f"{parts[0].lower()} {parts[1].lower()}" if len(parts) > 1 else ""
        if compound not in ALLOWED_SUBCOMMANDS:
            return False, f"Subcommand '{subcommand}' is not in allowlist"

    return True, "Command is safe"
