"""
LangGraph state machine for the K8s troubleshooting agent.

Flow:
  router → fetch_resources → summarize → rag_retrieve → analyze → suggest_commands → END

Conditional edges handle cluster availability and RAG skip logic.
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from langgraph.graph import END, StateGraph

from ..config.settings import get_settings
from .nodes import (
    analyze_node,
    fetch_resources_node,
    rag_retrieve_node,
    router_node,
    suggest_commands_node,
    summarize_node,
)
from .state import AgentState

logger = logging.getLogger(__name__)


def _should_fetch_resources(state: AgentState) -> str:
    """Conditional edge: fetch cluster data or skip straight to RAG."""
    if state.get("needs_cluster_data"):
        return "fetch_resources"
    return "summarize"  # Skip to empty summarize, then RAG


def _should_run_rag(state: AgentState) -> str:
    """Conditional edge: run RAG or go straight to analyze."""
    if state.get("needs_rag"):
        return "rag_retrieve"
    return "analyze"


class K8sAgentGraph:
    """
    Compiled LangGraph for Kubernetes troubleshooting.
    Reusable across CLI and MCP server.
    """

    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self._graph = self._build_graph()

    def _build_graph(self):
        """Build and compile the LangGraph state machine."""
        graph = StateGraph(AgentState)

        # Register nodes
        graph.add_node("router", router_node)
        graph.add_node("fetch_resources", fetch_resources_node)
        graph.add_node("summarize", summarize_node)
        graph.add_node("rag_retrieve", rag_retrieve_node)
        graph.add_node("analyze", analyze_node)
        graph.add_node("suggest_commands", suggest_commands_node)

        # Entry point
        graph.set_entry_point("router")

        # Edges
        graph.add_conditional_edges(
            "router",
            _should_fetch_resources,
            {
                "fetch_resources": "fetch_resources",
                "summarize": "summarize",
            },
        )
        graph.add_edge("fetch_resources", "summarize")
        graph.add_conditional_edges(
            "summarize",
            _should_run_rag,
            {
                "rag_retrieve": "rag_retrieve",
                "analyze": "analyze",
            },
        )
        graph.add_edge("rag_retrieve", "analyze")
        graph.add_edge("analyze", "suggest_commands")
        graph.add_edge("suggest_commands", END)

        return graph.compile()

    async def run(
        self,
        query: str,
        namespace: str = "default",
        resource_name: Optional[str] = None,
        resource_type: Optional[str] = None,
        output_format: str = "table",
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Run the full troubleshooting workflow.

        Returns the final AgentState as a dict.
        """
        start_time = time.monotonic()

        initial_state: AgentState = {
            "query": query,
            "namespace": namespace,
            "resource_name": resource_name,
            "resource_type": resource_type,
            "needs_cluster_data": False,
            "needs_rag": True,
            "cluster_reachable": False,
            "raw_pod_data": None,
            "raw_deployment_data": None,
            "raw_node_data": None,
            "raw_service_data": None,
            "raw_pvc_data": None,
            "raw_events_json": None,
            "raw_logs": {},
            "raw_prev_logs": {},
            "raw_top_pod_output": None,
            "raw_top_nodes_output": None,
            "structured_context": None,
            "token_count": 0,
            "rag_context": None,
            "diagnosis": None,
            "llm_raw_response": None,
            "errors": [],
            "warnings": [],
            "steps_taken": [],
            "execution_time_ms": 0,
            "output_format": output_format,
            "verbose": verbose,
        }

        try:
            final_state = await self._graph.ainvoke(initial_state)
        except Exception as exc:
            logger.exception("Agent graph execution failed: %s", exc)
            final_state = {
                **initial_state,
                "errors": [f"Agent execution failed: {exc}"],
                "diagnosis": None,
            }

        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        final_state["execution_time_ms"] = elapsed_ms

        logger.info(
            "Agent completed in %dms | steps=%s | tokens=%d",
            elapsed_ms,
            " → ".join(final_state.get("steps_taken", [])),
            final_state.get("token_count", 0),
        )

        return final_state

    def run_sync(
        self,
        query: str,
        namespace: str = "default",
        resource_name: Optional[str] = None,
        resource_type: Optional[str] = None,
        output_format: str = "table",
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """Synchronous wrapper for non-async contexts."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already in an event loop (e.g., Jupyter) — use nest_asyncio
                import nest_asyncio
                nest_asyncio.apply()
            return loop.run_until_complete(
                self.run(query, namespace, resource_name, resource_type, output_format, verbose)
            )
        except RuntimeError:
            # No event loop — create one
            return asyncio.run(
                self.run(query, namespace, resource_name, resource_type, output_format, verbose)
            )


# Module-level singleton
_agent_graph: Optional[K8sAgentGraph] = None


def get_agent() -> K8sAgentGraph:
    """Get or create the singleton agent graph."""
    global _agent_graph
    if _agent_graph is None:
        _agent_graph = K8sAgentGraph()
    return _agent_graph
