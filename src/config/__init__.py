"""Configuration package for k8s-ai-support."""

from .settings import Settings, get_settings
from .blocklist import BLOCKLIST_PATTERNS, is_command_safe

__all__ = ["Settings", "get_settings", "BLOCKLIST_PATTERNS", "is_command_safe"]
