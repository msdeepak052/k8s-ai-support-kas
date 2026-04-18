"""
MCP Protocol compliance tests.
Tests that the MCP server correctly handles JSON-RPC 2.0 messages.
"""

import asyncio
import json
import sys
import os
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def mcp_server():
    """Create an MCP server instance with mocked settings."""
    with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test", "K8S_AI_PROVIDER": "openai"}):
        from src.config.settings import reset_settings_cache
        reset_settings_cache()
        from src.cli.mcp_server import MCPServer
        server = MCPServer()
        return server


class TestMCPInitialize:
    """Test MCP initialize handshake."""

    @pytest.mark.asyncio
    async def test_initialize_returns_server_info(self, mcp_server):
        result = await mcp_server._handle_initialize({
            "clientInfo": {"name": "test-client", "version": "1.0"},
            "protocolVersion": "2024-11-05",
        })
        assert "protocolVersion" in result
        assert "serverInfo" in result
        assert result["serverInfo"]["name"] == "k8s-ai-support"
        assert "capabilities" in result
        assert "tools" in result["capabilities"]

    @pytest.mark.asyncio
    async def test_initialize_without_client_info(self, mcp_server):
        """Server should handle missing clientInfo gracefully."""
        result = await mcp_server._handle_initialize({})
        assert "protocolVersion" in result
        assert "serverInfo" in result


class TestMCPToolsList:
    """Test tools/list endpoint."""

    @pytest.mark.asyncio
    async def test_tools_list_returns_all_tools(self, mcp_server):
        result = await mcp_server._handle_tools_list({})
        assert "tools" in result
        tools = result["tools"]
        assert len(tools) >= 4

        tool_names = {t["name"] for t in tools}
        assert "k8s_diagnose" in tool_names
        assert "k8s_get_resources" in tool_names
        assert "k8s_get_logs" in tool_names
        assert "k8s_describe" in tool_names

    @pytest.mark.asyncio
    async def test_each_tool_has_required_fields(self, mcp_server):
        result = await mcp_server._handle_tools_list({})
        for tool in result["tools"]:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"
            assert "properties" in tool["inputSchema"]

    @pytest.mark.asyncio
    async def test_k8s_diagnose_schema(self, mcp_server):
        result = await mcp_server._handle_tools_list({})
        diagnose_tool = next(t for t in result["tools"] if t["name"] == "k8s_diagnose")
        schema = diagnose_tool["inputSchema"]
        assert "query" in schema["properties"]
        assert "namespace" in schema["properties"]
        assert "required" in schema
        assert "query" in schema["required"]


class TestMCPToolsCall:
    """Test tools/call endpoint."""

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self, mcp_server):
        result = await mcp_server._handle_tools_call({
            "name": "nonexistent_tool",
            "arguments": {},
        })
        assert result["isError"] is True

    @pytest.mark.asyncio
    async def test_call_k8s_diagnose_missing_query(self, mcp_server):
        result = await mcp_server._handle_tools_call({
            "name": "k8s_diagnose",
            "arguments": {"namespace": "default"},
        })
        # Should return error for missing query
        content = json.loads(result["content"][0]["text"])
        assert "error" in content

    @pytest.mark.asyncio
    async def test_call_k8s_diagnose_success(self, mcp_server):
        """Test full diagnose tool call with mocked agent."""
        mock_state = {
            "diagnosis": {
                "diagnosis": {
                    "root_cause": "CrashLoopBackOff due to missing config",
                    "confidence": 0.85,
                    "affected_resources": ["pod/test"],
                    "severity": "high",
                    "category": "crashloop",
                },
                "analysis": "Container crashes on startup",
                "suggestions": [],
                "additional_checks": [],
                "estimated_fix_time": "quick (< 5 min)",
            },
            "warnings": [],
            "errors": [],
            "execution_time_ms": 1200,
            "token_count": 500,
            "cluster_reachable": False,
        }

        with patch.object(mcp_server, "_get_agent") as mock_get_agent:
            mock_agent = AsyncMock()
            mock_agent.run.return_value = mock_state
            mock_get_agent.return_value = mock_agent

            result = await mcp_server._handle_tools_call({
                "name": "k8s_diagnose",
                "arguments": {"query": "pod is crashing", "namespace": "default"},
            })

        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert "diagnosis" in content

    @pytest.mark.asyncio
    async def test_call_k8s_get_resources_success(self, mcp_server):
        """Test get_resources with mocked kubectl."""
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.is_not_found = False
        mock_result.is_timeout = False
        mock_result.parsed = {
            "kind": "PodList",
            "items": [
                {
                    "metadata": {"name": "test-pod", "namespace": "default", "creationTimestamp": "2024-01-01T00:00:00Z"},
                    "status": {"phase": "Running", "conditions": []},
                }
            ],
        }

        with patch.object(mcp_server.kubectl, "get_resource", return_value=mock_result):
            result = await mcp_server._handle_tools_call({
                "name": "k8s_get_resources",
                "arguments": {"resource_type": "pods", "namespace": "default"},
            })

        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert "items" in content

    @pytest.mark.asyncio
    async def test_call_k8s_get_logs_success(self, mcp_server):
        """Test get_logs with mocked kubectl."""
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.is_not_found = False
        mock_result.stdout = "line 1\nline 2\nERROR: connection refused\n"

        with patch.object(mcp_server.kubectl, "get_logs", return_value=mock_result):
            result = await mcp_server._handle_tools_call({
                "name": "k8s_get_logs",
                "arguments": {"pod_name": "test-pod", "namespace": "default"},
            })

        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert content["pod"] == "test-pod"
        assert "connection refused" in content["logs"]

    @pytest.mark.asyncio
    async def test_call_k8s_get_logs_missing_pod(self, mcp_server):
        """Test get_logs with missing pod_name."""
        result = await mcp_server._handle_tools_call({
            "name": "k8s_get_logs",
            "arguments": {"namespace": "default"},
        })
        content = json.loads(result["content"][0]["text"])
        assert "error" in content


class TestMCPRateLimiting:
    """Test rate limiting functionality."""

    def test_rate_limiter_allows_under_limit(self, mcp_server):
        for _ in range(5):
            assert mcp_server.rate_limiter.is_allowed() is True

    def test_rate_limiter_blocks_over_limit(self):
        from src.cli.mcp_server import RateLimiter
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            limiter.is_allowed()
        # 4th request should be blocked
        assert limiter.is_allowed() is False

    def test_rate_limiter_resets_after_window(self):
        from src.cli.mcp_server import RateLimiter
        import time
        limiter = RateLimiter(max_requests=2, window_seconds=1)
        limiter.is_allowed()
        limiter.is_allowed()
        # Should be blocked
        assert limiter.is_allowed() is False
        # Wait for window to expire
        time.sleep(1.1)
        # Should be allowed again
        assert limiter.is_allowed() is True


class TestMCPProtocol:
    """Test JSON-RPC 2.0 protocol compliance."""

    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(self, mcp_server):
        response = await mcp_server._handle_request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "unknown/method",
            "params": {},
        })
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "error" in response
        assert response["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_ping_returns_empty_result(self, mcp_server):
        response = await mcp_server._handle_request({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "ping",
            "params": {},
        })
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 2
        assert "result" in response
        assert response["result"] == {}

    @pytest.mark.asyncio
    async def test_initialized_notification_returns_none(self, mcp_server):
        """Notifications (no id) should return None (no response)."""
        response = await mcp_server._handle_request({
            "jsonrpc": "2.0",
            "method": "initialized",
            "params": {},
        })
        assert response is None

    @pytest.mark.asyncio
    async def test_tools_list_via_handle_request(self, mcp_server):
        response = await mcp_server._handle_request({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/list",
            "params": {},
        })
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 3
        assert "result" in response
        assert "tools" in response["result"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
