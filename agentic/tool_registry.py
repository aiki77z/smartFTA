from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional


ToolCallable = Callable[..., Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    stage: str
    memory_type: Optional[str] = None
    func: Optional[ToolCallable] = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}
        self._allowlist: Dict[str, List[str]] = {}

    def register(self, spec: ToolSpec, *, agents: Iterable[str]) -> None:
        self._tools[spec.name] = spec
        for agent in agents:
            self._allowlist.setdefault(agent, [])
            if spec.name not in self._allowlist[agent]:
                self._allowlist[agent].append(spec.name)

    def get(self, name: str) -> ToolSpec:
        return self._tools[name]

    def list_for_agent(self, agent: str) -> List[ToolSpec]:
        return [self._tools[name] for name in self._allowlist.get(agent, []) if name in self._tools]

    def assert_allowed(self, agent: str, tool_name: str) -> None:
        if tool_name not in self._allowlist.get(agent, []):
            raise PermissionError(f"{agent} is not allowed to call tool: {tool_name}")


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec("parse_prompt", "Extract top event and requirements from the user prompt.", "scope"),
        agents=["ScopeAgent"],
    )
    registry.register(
        ToolSpec("match_graph_top_event", "Resolve the selected top event against graph/catalog memory.", "graph_match", "domain"),
        agents=["ScopeAgent", "RetrievalAgent"],
    )
    registry.register(
        ToolSpec("expand_subgraph", "Expand the scoped local fault subgraph.", "graph_subgraph", "domain"),
        agents=["RetrievalAgent"],
    )
    registry.register(
        ToolSpec("collect_chunks", "Collect evidence chunks for the retrieved subgraph.", "graph_chunks", "domain"),
        agents=["RetrievalAgent"],
    )
    registry.register(
        ToolSpec("build_tree_draft", "Build a draft fault tree from retrieval context.", "generate_draft"),
        agents=["TreeDraftAgent"],
    )
    registry.register(
        ToolSpec(
            "assess_retrieval_context",
            "Assess whether the retrieved graph skeleton is complete enough to guide draft generation.",
            "generate_draft",
            "artifact",
        ),
        agents=["TreeDraftAgent"],
    )
    registry.register(
        ToolSpec("build_tree_from_subgraph", "Build a draft fault tree from the retrieved graph skeleton and chunks.", "generate_draft", "artifact"),
        agents=["TreeDraftAgent"],
    )
    registry.register(
        ToolSpec("build_tree_from_chunks", "Build a draft fault tree from evidence chunks when graph context is weak.", "generate_draft", "artifact"),
        agents=["TreeDraftAgent"],
    )
    registry.register(
        ToolSpec("validate_tree", "Validate structure, semantics, and evidence.", "validate"),
        agents=["VerifyAgent", "RepairAgent"],
    )
    registry.register(
        ToolSpec("validate_tree_full", "Run the required full validation quality gate.", "validate", "artifact"),
        agents=["VerifyAgent"],
    )
    registry.register(
        ToolSpec("validate_tree_structural", "Run a structural validation pre-check.", "validate", "artifact"),
        agents=["VerifyAgent"],
    )
    registry.register(
        ToolSpec("find_repair_patterns", "Search active expert repair patterns.", "repair", "experience"),
        agents=["RepairAgent", "ExperienceMemoryAgent"],
    )
    registry.register(
        ToolSpec("get_relevant_corrections", "Retrieve expert correction episodes relevant to the draft tree.", "repair", "experience"),
        agents=["RepairAgent", "ExperienceMemoryAgent"],
    )
    registry.register(
        ToolSpec("apply_repair_patch", "Apply a repair patch to a draft tree.", "repair"),
        agents=["RepairAgent"],
    )
    registry.register(
        ToolSpec("save_tree_version", "Persist a validated fault tree version.", "persistence"),
        agents=["CommitAgent"],
    )
    return registry
