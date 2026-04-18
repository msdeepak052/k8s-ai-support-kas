"""
MCP (Model Context Protocol) Server — JSON-RPC over stdio.
Exposes Kubernetes troubleshooting as tools for VS Code, Cursor, Kiro, etc.

Protocol: https://modelcontextprotocol.io/specification
Transport: stdio (JSON-RPC 2.0 messages, newline-delimited)
"""

import asyncio
import json
import logging
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..agent.graph import get_agent
from ..config.settings import get_settings
from ..tools.kubectl_wrapper import KubectlWrapper

logger = logging.getLogger(__name__)

# MCP Protocol version
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "k8s-ai-support"
SERVER_VERSION = "1.0.0"


# ─────────────────────────── MCP Tool Definitions ───────────────────────────

MCP_TOOLS = [
    {
        "name": "k8s_diagnose",
        "description": (
            "Diagnose a Kubernetes issue using AI. Analyzes live cluster state "
            "and Kubernetes documentation to identify root causes and suggest fixes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language description of the Kubernetes issue",
                },
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace to investigate (default: 'default')",
                    "default": "default",
                },
                "resource_name": {
                    "type": "string",
                    "description": "Optional: specific resource name (e.g., 'nginx-pod-xyz')",
                },
                "resource_type": {
                    "type": "string",
                    "description": "Optional: resource type (pod, deployment, service, node, pvc)",
                    "enum": ["pod", "deployment", "service", "node", "pvc", "ingress", "statefulset", "daemonset"],
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "k8s_get_resources",
        "description": "List Kubernetes resources (read-only). Returns structured summary of cluster resources.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource_type": {
                    "type": "string",
                    "description": "Resource type to list",
                    "enum": [
                        "pods", "deployments", "services", "nodes", "ingresses",
                        "persistentvolumeclaims", "configmaps", "events",
                        "statefulsets", "daemonsets", "jobs", "cronjobs",
                        "replicasets", "horizontalpodautoscalers", "namespaces",
                    ],
                },
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace (use 'all' for all namespaces)",
                    "default": "default",
                },
                "name": {
                    "type": "string",
                    "description": "Optional: specific resource name",
                },
            },
            "required": ["resource_type"],
        },
    },
    {
        "name": "k8s_get_logs",
        "description": "Fetch pod logs for debugging. Returns last 100 lines and previous crash logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pod_name": {
                    "type": "string",
                    "description": "Name of the pod",
                },
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace",
                    "default": "default",
                },
                "container": {
                    "type": "string",
                    "description": "Optional: container name (for multi-container pods)",
                },
                "previous": {
                    "type": "boolean",
                    "description": "Get logs from previous container instance (for crash investigation)",
                    "default": False,
                },
            },
            "required": ["pod_name"],
        },
    },
    {
        "name": "k8s_describe",
        "description": "Describe a Kubernetes resource in detail (equivalent to kubectl describe).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource_type": {
                    "type": "string",
                    "description": "Resource type (pod, deployment, service, node, pvc, etc.)",
                },
                "name": {
                    "type": "string",
                    "description": "Resource name",
                },
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace",
                    "default": "default",
                },
            },
            "required": ["resource_type", "name"],
        },
    },
    {
        "name": "k8s_get_events",
        "description": "Get Kubernetes events, optionally filtered by resource. Events show warnings and errors.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace",
                    "default": "default",
                },
                "resource_name": {
                    "type": "string",
                    "description": "Optional: filter events for a specific resource name",
                },
            },
            "required": [],
        },
    },
]


# ─────────────────────────── Rate Limiter ───────────────────────────────────

class RateLimiter:
    """Simple sliding window rate limiter."""

    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: deque = deque()

    def is_allowed(self) -> bool:
        now = time.monotonic()
        # Remove old requests outside window
        while self._requests and self._requests[0] < now - self.window_seconds:
            self._requests.popleft()

        if len(self._requests) >= self.max_requests:
            return False

        self._requests.append(now)
        return True


# ─────────────────────────── MCP Server ─────────────────────────────────────

class MCPServer:
    """
    MCP JSON-RPC server over stdio.
    Handles: initialize, tools/list, tools/call methods.
    """

    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self.kubectl = KubectlWrapper(settings=self.settings)
        self.rate_limiter = RateLimiter(
            max_requests=self.settings.mcp_rate_limit,
            window_seconds=60,
        )
        self._agent = None

    def _get_agent(self):
        if self._agent is None:
            self._agent = get_agent()
        return self._agent

    async def start(self):
        """Start reading JSON-RPC messages from stdin and writing to stdout."""
        logger.info("MCP server starting on stdio")

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_event_loop()

        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        transport, _ = await loop.connect_write_pipe(asyncio.BaseProtocol, sys.stdout.buffer)

        async def write_response(data: dict):
            msg = json.dumps(data) + "\n"
            sys.stdout.buffer.write(msg.encode("utf-8"))
            sys.stdout.buffer.flush()

        logger.info("MCP server ready, listening on stdin")

        while True:
            try:
                line = await reader.readline()
                if not line:
                    logger.info("stdin closed, shutting down")
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    await write_response({
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {exc}"},
                    })
                    continue

                response = await self._handle_request(request)
                if response is not None:
                    await write_response(response)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Unexpected error in MCP loop: %s", exc)

    async def _handle_request(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Route JSON-RPC request to handler."""
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        # Notification (no id) — fire and forget
        if req_id is None and method.startswith("notifications/"):
            return None

        try:
            if method == "initialize":
                result = await self._handle_initialize(params)
            elif method == "initialized":
                return None  # Notification, no response
            elif method == "tools/list":
                result = await self._handle_tools_list(params)
            elif method == "tools/call":
                result = await self._handle_tools_call(params)
            elif method == "ping":
                result = {}
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }

            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        except Exception as exc:
            logger.exception("Error handling method %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }

    async def _handle_initialize(self, params: Dict) -> Dict:
        """MCP initialize handshake."""
        client_info = params.get("clientInfo", {})
        logger.info(
            "MCP client connecting: %s %s",
            client_info.get("name", "unknown"),
            client_info.get("version", ""),
        )
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        }

    async def _handle_tools_list(self, params: Dict) -> Dict:
        """Return list of available MCP tools."""
        return {"tools": MCP_TOOLS}

    async def _handle_tools_call(self, params: Dict) -> Dict:
        """Execute a tool call."""
        # Rate limiting
        if not self.rate_limiter.is_allowed():
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({
                            "error": "rate_limit_exceeded",
                            "message": f"Rate limit: max {self.settings.mcp_rate_limit} requests/minute",
                        }),
                    }
                ],
                "isError": True,
            }

        tool_name = params.get("name", "")
        tool_input = params.get("arguments", {})

        logger.info("MCP tool call: %s | input=%s", tool_name, tool_input)

        try:
            if tool_name == "k8s_diagnose":
                result = await self._tool_k8s_diagnose(tool_input)
            elif tool_name == "k8s_get_resources":
                result = await self._tool_k8s_get_resources(tool_input)
            elif tool_name == "k8s_get_logs":
                result = await self._tool_k8s_get_logs(tool_input)
            elif tool_name == "k8s_describe":
                result = await self._tool_k8s_describe(tool_input)
            elif tool_name == "k8s_get_events":
                result = await self._tool_k8s_get_events(tool_input)
            else:
                return {
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                }

            return {
                "content": [{"type": "text", "text": result}],
                "isError": False,
            }

        except Exception as exc:
            logger.exception("Tool call failed: %s | %s", tool_name, exc)
            return {
                "content": [
                    {"type": "text", "text": json.dumps({"error": str(exc)})}
                ],
                "isError": True,
            }

    # ─────────────── Tool Implementations ───────────────────────────────────

    async def _tool_k8s_diagnose(self, inputs: Dict) -> str:
        """Run full AI diagnosis on a K8s issue."""
        query = inputs.get("query", "")
        namespace = inputs.get("namespace", "default")
        resource_name = inputs.get("resource_name")
        resource_type = inputs.get("resource_type")

        if not query:
            return json.dumps({"error": "query parameter is required"})

        agent = self._get_agent()
        state = await agent.run(
            query=query,
            namespace=namespace,
            resource_name=resource_name,
            resource_type=resource_type,
            output_format="json",
        )

        response = {
            "diagnosis": state.get("diagnosis"),
            "warnings": state.get("warnings", []),
            "errors": state.get("errors", []),
            "execution_time_ms": state.get("execution_time_ms", 0),
            "tokens_used": state.get("token_count", 0),
            "cluster_reachable": state.get("cluster_reachable", False),
        }
        return json.dumps(response, indent=2, default=str)

    async def _tool_k8s_get_resources(self, inputs: Dict) -> str:
        """List K8s resources and return structured summary."""
        resource_type = inputs.get("resource_type", "pods")
        namespace = inputs.get("namespace", "default")
        name = inputs.get("name")

        result = await self.kubectl.get_resource(resource_type, name, namespace)

        if not result.success:
            if result.is_not_found:
                return json.dumps({"error": f"Resource not found: {resource_type}/{name or ''}"})
            if result.is_timeout:
                return json.dumps({"error": "kubectl command timed out"})
            return json.dumps({"error": result.stderr or result.error_message})

        # Return parsed JSON if available, else raw text
        if result.parsed:
            # Extract just the essential fields for compactness
            items = result.parsed
            if items.get("kind", "").endswith("List"):
                summary = []
                for item in items.get("items", [])[:20]:  # Max 20 items
                    meta = item.get("metadata", {})
                    status = item.get("status", {})
                    summary.append({
                        "name": meta.get("name"),
                        "namespace": meta.get("namespace"),
                        "phase": status.get("phase") or item.get("status", {}).get("conditions", [{}])[-1].get("type"),
                        "age": meta.get("creationTimestamp"),
                    })
                return json.dumps({
                    "resource_type": resource_type,
                    "namespace": namespace,
                    "count": len(items.get("items", [])),
                    "items": summary,
                }, indent=2, default=str)
            else:
                return json.dumps(result.parsed, indent=2, default=str)
        else:
            return result.stdout[:5000]  # Limit plain text output

    async def _tool_k8s_get_logs(self, inputs: Dict) -> str:
        """Fetch pod logs."""
        pod_name = inputs.get("pod_name", "")
        namespace = inputs.get("namespace", "default")
        container = inputs.get("container")
        previous = inputs.get("previous", False)

        if not pod_name:
            return json.dumps({"error": "pod_name is required"})

        result = await self.kubectl.get_logs(
            pod_name=pod_name,
            namespace=namespace,
            container=container,
            previous=previous,
            tail=100,
        )

        if not result.success:
            if result.is_not_found:
                return json.dumps({"error": f"Pod not found: {pod_name}"})
            return json.dumps({"error": result.stderr or result.error_message})

        logs = result.stdout
        lines = logs.split("\n")

        return json.dumps({
            "pod": pod_name,
            "namespace": namespace,
            "container": container,
            "previous": previous,
            "line_count": len(lines),
            "logs": "\n".join(lines[-100:]),  # Last 100 lines
        }, indent=2)

    async def _tool_k8s_describe(self, inputs: Dict) -> str:
        """Describe a K8s resource."""
        resource_type = inputs.get("resource_type", "pod")
        name = inputs.get("name", "")
        namespace = inputs.get("namespace", "default")

        if not name:
            return json.dumps({"error": "name is required"})

        result = await self.kubectl.describe_resource(resource_type, name, namespace)

        if not result.success:
            if result.is_not_found:
                return json.dumps({"error": f"Resource not found: {resource_type}/{name}"})
            return json.dumps({"error": result.stderr or result.error_message})

        # Return structured text (describe output is already human-readable)
        return json.dumps({
            "resource_type": resource_type,
            "name": name,
            "namespace": namespace,
            "description": result.stdout[:8000],  # Limit length
        }, indent=2)

    async def _tool_k8s_get_events(self, inputs: Dict) -> str:
        """Get Kubernetes events."""
        namespace = inputs.get("namespace", "default")
        resource_name = inputs.get("resource_name")

        result = await self.kubectl.get_events(namespace=namespace, resource_name=resource_name)

        if not result.success:
            return json.dumps({"error": result.stderr or result.error_message})

        events = result.parsed or {}
        items = events.get("items", [])

        # Summarize events
        summary = []
        for evt in items[:20]:  # Max 20 events
            summary.append({
                "type": evt.get("type"),
                "reason": evt.get("reason"),
                "message": evt.get("message", "")[:200],
                "count": evt.get("count", 1),
                "lastTimestamp": evt.get("lastTimestamp"),
                "involvedObject": f"{evt.get('involvedObject', {}).get('kind')}/{evt.get('involvedObject', {}).get('name')}",
            })

        return json.dumps({
            "namespace": namespace,
            "event_count": len(items),
            "events": summary,
        }, indent=2, default=str)
