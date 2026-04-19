"""
LangGraph state definitions for the K8s troubleshooting agent.
TypedDict-based state machine — immutable, serializable.
"""

from typing import Any, Dict, List, Optional, TypedDict


class AgentState(TypedDict, total=False):
    """
    State object passed through the LangGraph state machine.
    Each node reads from and writes to this state.
    """

    # Input
    query: str
    namespace: str
    resource_name: Optional[str]
    resource_type: Optional[str]

    # Routing decisions
    needs_cluster_data: bool
    needs_rag: bool
    cluster_reachable: bool

    # Raw kubectl results
    raw_pod_data: Optional[List[Dict[str, Any]]]        # list of pod JSONs
    raw_deployment_data: Optional[List[Dict[str, Any]]]
    raw_node_data: Optional[List[Dict[str, Any]]]
    raw_service_data: Optional[List[Dict[str, Any]]]
    raw_pvc_data: Optional[List[Dict[str, Any]]]
    raw_events_json: Optional[Dict[str, Any]]
    raw_logs: Optional[Dict[str, str]]          # pod_name -> log_text
    raw_prev_logs: Optional[Dict[str, str]]     # pod_name -> previous log text
    raw_top_pod_output: Optional[str]           # stdout of "kubectl top pod <name>"
    raw_top_nodes_output: Optional[str]         # stdout of "kubectl top nodes"

    # Summarized context
    structured_context: Optional[Dict[str, Any]]
    token_count: int

    # RAG results
    rag_context: Optional[str]

    # LLM output
    diagnosis: Optional[Dict[str, Any]]
    llm_raw_response: Optional[str]

    # Execution metadata
    errors: List[str]
    warnings: List[str]
    steps_taken: List[str]
    execution_time_ms: int

    # Output control
    output_format: str   # json, yaml, table
    verbose: bool
