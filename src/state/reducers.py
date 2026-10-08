"""LangGraph channel reducers shared by every agent's state."""
from typing import List

from src.agents.commons.models import NodeReport


def append_node_reports(existing: List[NodeReport], new: List[NodeReport]) -> List[NodeReport]:
    """Append each node's report to the cycle's reasoning trail.

    Usage in a state class:
        node_reports: Annotated[list[NodeReport], append_node_reports]
    """
    existing = existing or []
    new = new or []
    return existing + new
