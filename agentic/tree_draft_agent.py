from __future__ import annotations

import json
from typing import Any, Dict, List

from openai import OpenAI

from config import ENABLE_GRAPH_RETRIEVAL, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PARSE_MAX_TOKENS
from generator import generate_fault_tree_draft

from .tool_events import (
    EVENT_AGENT_COMPLETED,
    EVENT_AGENT_STARTED,
    EVENT_TOOL_COMPLETED,
    EVENT_TOOL_FAILED,
    EVENT_TOOL_SELECTED,
    EVENT_TOOL_STARTED,
    append_agentic_event,
    append_tool_event,
)
from .tool_registry import ToolRegistry, build_default_registry


client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


class TreeDraftAgent:
    name = "TreeDraftAgent"

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()

    def run(self, state: Dict[str, Any], catalog: Dict[str, Any], retrieval_context: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        top_event = str(state.get("resolved_top_event") or catalog.get("name") or state.get("requested_top_event") or "")
        options = state.get("options") if isinstance(state.get("options"), dict) else {}
        draft_attempt = int(state.get("draft_attempt_count") or 0) + 1
        requirements = _compose_generation_requirements(
            str(state.get("requirements") or ""),
            state.get("validation_report") if isinstance(state.get("validation_report"), dict) else {},
            draft_attempt=draft_attempt,
        )
        append_agentic_event(
            run_id,
            EVENT_AGENT_STARTED,
            agent=self.name,
            stage="generate_draft",
            message="TreeDraftAgent started autonomous draft planning.",
            progress=44,
            details={
                "top_event": top_event,
                "draft_attempt": draft_attempt,
                "subgraph_node_count": len((retrieval_context.get("subgraph_bundle") or {}).get("nodes") or []),
                "evidence_chunk_count": len(retrieval_context.get("raw_chunks") or []),
            },
        )
        context_assessment = self._assess_retrieval_context(run_id, retrieval_context)
        tool = self._select_build_tool(retrieval_context, context_assessment)
        append_agentic_event(
            run_id,
            EVENT_TOOL_SELECTED,
            agent=self.name,
            stage="generate_draft",
            message=f"TreeDraftAgent selected draft tool: {tool}.",
            progress=45,
            tool=tool,
            details={"tool": tool, "context_assessment": context_assessment},
        )
        draft_tree = self._call_build_tool(
            run_id,
            tool,
            top_event=top_event,
            requirements=requirements,
            selected_file_version_ids=list(state.get("selected_file_version_ids") or []),
            root_graph_node_id=state.get("graph_node_id") or catalog.get("graph_node_id"),
            retrieval_context=retrieval_context,
            part_details=options.get("part_details") if isinstance(options, dict) else None,
            max_depth=options.get("max_depth") if isinstance(options, dict) else None,
        )
        append_agentic_event(
            run_id,
            EVENT_AGENT_COMPLETED,
            agent=self.name,
            stage="generate_draft",
            message="TreeDraftAgent completed draft fault tree.",
            progress=50,
            details={
                "node_count": len(draft_tree.get("nodeList") or []),
                "link_count": len(draft_tree.get("linkList") or []),
                "selected_tool": tool,
            },
        )
        return draft_tree

    def _assess_retrieval_context(self, run_id: str, retrieval_context: Dict[str, Any]) -> Dict[str, Any]:
        tool = "assess_retrieval_context"
        self.registry.assert_allowed(self.name, tool)
        append_agentic_event(
            run_id,
            EVENT_TOOL_SELECTED,
            agent=self.name,
            stage="generate_draft",
            message="TreeDraftAgent selected context assessment tool: assess_retrieval_context.",
            progress=44,
            tool=tool,
        )
        append_tool_event(
            run_id,
            EVENT_TOOL_STARTED,
            agent=self.name,
            tool=tool,
            stage="generate_draft",
            message="TreeDraftAgent started tool: assess_retrieval_context.",
            progress=44,
        )
        try:
            assessment = self._llm_assess_retrieval_context(retrieval_context)
        except Exception as exc:
            append_tool_event(
                run_id,
                EVENT_TOOL_FAILED,
                agent=self.name,
                tool=tool,
                stage="generate_draft",
                message="TreeDraftAgent context assessment failed; using deterministic fallback.",
                reason=str(exc),
            )
            assessment = _fallback_context_assessment(retrieval_context)
        append_tool_event(
            run_id,
            EVENT_TOOL_COMPLETED,
            agent=self.name,
            tool=tool,
            stage="generate_draft",
            message=(
                "TreeDraftAgent assessed retrieval context: "
                f"{assessment.get('recommended_strategy')}."
            ),
            progress=45,
            details=assessment,
        )
        return assessment

    def _llm_assess_retrieval_context(self, retrieval_context: Dict[str, Any]) -> Dict[str, Any]:
        fallback = _fallback_context_assessment(retrieval_context)
        if not LLM_API_KEY:
            return fallback
        subgraph = retrieval_context.get("subgraph_bundle") or {}
        nodes = subgraph.get("nodes") or []
        edges = subgraph.get("edges") or []
        chunks = retrieval_context.get("raw_chunks") or []
        prompt = {
            "role": "user",
            "content": (
                "You are TreeDraftAgent assessing retrieval context for fault-tree generation. "
                "Prefer graph-skeleton generation when the graph has a plausible causal skeleton, "
                "even if evidence chunks are also useful. Choose evidence-only chunks only when "
                "the graph is clearly empty, broken, or too weak to guide structure.\n"
                "Return only JSON with keys: "
                "subgraph_usable(boolean), coverage_score(number 0-1), evidence_support_score(number 0-1), "
                "noise_risk(low|medium|high), recommended_strategy(build_tree_from_subgraph|build_tree_from_chunks), "
                "reason(string), missing_context(array).\n"
                f"Root count: {len(subgraph.get('roots') or [])}\n"
                f"Graph node count: {len(nodes)}\n"
                f"Graph edge count: {len(edges)}\n"
                f"Gate group count: {len(subgraph.get('gate_groups') or [])}\n"
                f"Disabled cycle edge count: {len(subgraph.get('disabled_cycle_edges') or [])}\n"
                f"Pruned transitive edge count: {len(subgraph.get('pruned_transitive_edges') or [])}\n"
                f"Evidence chunk count: {len(chunks)}\n"
                f"Sample graph nodes: {json.dumps(_sample_graph_nodes(nodes), ensure_ascii=False)}\n"
                f"Sample graph edges: {json.dumps(_sample_graph_edges(edges), ensure_ascii=False)}\n"
                f"Sample chunks: {json.dumps(_sample_chunks(chunks), ensure_ascii=False)}"
            ),
        }
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[prompt],
            temperature=0.0,
            max_tokens=min(LLM_PARSE_MAX_TOKENS, 300),
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
        if not isinstance(parsed, dict):
            return fallback
        strategy = str(parsed.get("recommended_strategy") or "")
        if strategy not in {"build_tree_from_subgraph", "build_tree_from_chunks"}:
            strategy = str(fallback.get("recommended_strategy") or "build_tree_from_subgraph")
        return {
            "subgraph_usable": bool(parsed.get("subgraph_usable", fallback.get("subgraph_usable"))),
            "coverage_score": _bounded_float(parsed.get("coverage_score"), fallback.get("coverage_score", 0.0)),
            "evidence_support_score": _bounded_float(
                parsed.get("evidence_support_score"),
                fallback.get("evidence_support_score", 0.0),
            ),
            "noise_risk": str(parsed.get("noise_risk") or fallback.get("noise_risk") or "medium"),
            "recommended_strategy": strategy,
            "reason": str(parsed.get("reason") or fallback.get("reason") or ""),
            "missing_context": parsed.get("missing_context") if isinstance(parsed.get("missing_context"), list) else [],
            "fallback_strategy": fallback.get("recommended_strategy"),
        }

    def _select_build_tool(self, retrieval_context: Dict[str, Any], assessment: Dict[str, Any]) -> str:
        build_tools = {"build_tree_from_subgraph", "build_tree_from_chunks", "build_tree_draft"}
        allowed = [tool.name for tool in self.registry.list_for_agent(self.name) if tool.name in build_tools]
        subgraph = retrieval_context.get("subgraph_bundle") or {}
        graph_ready = bool(ENABLE_GRAPH_RETRIEVAL and subgraph.get("nodes") and subgraph.get("edges"))
        assessed_tool = str(assessment.get("recommended_strategy") or "")
        fallback = assessed_tool if assessed_tool in {"build_tree_from_subgraph", "build_tree_from_chunks"} else ""
        if not fallback:
            fallback = "build_tree_from_subgraph" if graph_ready else "build_tree_from_chunks"
        if not LLM_API_KEY:
            return fallback
        prompt = {
            "role": "user",
            "content": (
                "You are TreeDraftAgent for a fault-tree generation system. "
                "Choose exactly one tool name from the allowed list. "
                "Prefer build_tree_from_subgraph when the graph skeleton is usable; "
                "choose build_tree_from_chunks only when the graph is clearly too weak or broken. "
                f"Allowed tools: {allowed}\n"
                f"Graph node count: {len(subgraph.get('nodes') or [])}\n"
                f"Graph edge count: {len(subgraph.get('edges') or [])}\n"
                f"Evidence chunk count: {len(retrieval_context.get('raw_chunks') or [])}\n"
                f"Context assessment: {json.dumps(assessment, ensure_ascii=False)}\n"
                "Return only JSON: {\"tool\": \"...\"}"
            ),
        }
        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[prompt],
                temperature=0.0,
                max_tokens=min(LLM_PARSE_MAX_TOKENS, 120),
            )
            parsed = json.loads(response.choices[0].message.content or "{}")
            tool = str(parsed.get("tool") or "")
            if tool in allowed and tool in {"build_tree_from_subgraph", "build_tree_from_chunks", "build_tree_draft"}:
                return tool
        except Exception:
            return fallback
        return fallback

    def _call_build_tool(
        self,
        run_id: str,
        tool: str,
        *,
        top_event: str,
        requirements: str,
        selected_file_version_ids: List[str],
        root_graph_node_id: Any,
        retrieval_context: Dict[str, Any],
        part_details: Dict[str, Any] | None,
        max_depth: Any,
    ) -> Dict[str, Any]:
        self.registry.assert_allowed(self.name, tool)
        append_tool_event(
            run_id,
            EVENT_TOOL_STARTED,
            agent=self.name,
            tool=tool,
            stage="generate_draft",
            message=f"TreeDraftAgent started tool: {tool}.",
            progress=46,
        )
        try:
            draft_tree = generate_fault_tree_draft(
                top_event=top_event,
                requirements=requirements,
                selected_file_version_ids=selected_file_version_ids,
                root_graph_node_id=root_graph_node_id,
                part_details=part_details,
                max_depth=max_depth,
                retrieval_context=retrieval_context,
                draft_strategy="chunks" if tool == "build_tree_from_chunks" else "subgraph",
            )
        except Exception as exc:
            append_tool_event(
                run_id,
                EVENT_TOOL_FAILED,
                agent=self.name,
                tool=tool,
                stage="generate_draft",
                message=f"TreeDraftAgent tool failed: {tool}.",
                reason=str(exc),
            )
            raise
        append_tool_event(
            run_id,
            EVENT_TOOL_COMPLETED,
            agent=self.name,
            tool=tool,
            stage="generate_draft",
            message=f"TreeDraftAgent completed tool: {tool}.",
            progress=49,
            details={
                "node_count": len(draft_tree.get("nodeList") or []),
                "link_count": len(draft_tree.get("linkList") or []),
            },
        )
        return draft_tree


def _compose_generation_requirements(
    requirements: str,
    validation_report: Dict[str, Any],
    *,
    draft_attempt: int,
) -> str:
    if draft_attempt <= 1 or not isinstance(validation_report, dict):
        return requirements
    issues = [
        issue
        for issue in (validation_report.get("issues") or [])
        if isinstance(issue, dict) and issue.get("severity") == "error"
    ]
    if not issues:
        return requirements
    feedback = "\n".join(
        f"- {issue.get('issue_code')}: {issue.get('message')}"
        for issue in issues[:8]
    )
    return (
        f"{requirements}\n\n"
        "Previous draft failed validation. Rebuild the fault tree and avoid these validation errors:\n"
        f"{feedback}"
    ).strip()


def _fallback_context_assessment(retrieval_context: Dict[str, Any]) -> Dict[str, Any]:
    subgraph = retrieval_context.get("subgraph_bundle") or {}
    nodes = subgraph.get("nodes") or []
    edges = subgraph.get("edges") or []
    chunks = retrieval_context.get("raw_chunks") or []
    graph_ready = bool(ENABLE_GRAPH_RETRIEVAL and nodes and edges)
    node_count = len(nodes)
    edge_count = len(edges)
    chunk_count = len(chunks)
    coverage_score = min(1.0, (node_count / 8.0) * 0.6 + (edge_count / 7.0) * 0.4) if graph_ready else 0.0
    evidence_support_score = min(1.0, chunk_count / 6.0) if chunk_count else 0.0
    graph_clearly_weak = not graph_ready or node_count < 2 or edge_count < 1
    return {
        "subgraph_usable": not graph_clearly_weak,
        "coverage_score": round(coverage_score, 3),
        "evidence_support_score": round(evidence_support_score, 3),
        "noise_risk": "low" if graph_ready and edge_count <= max(node_count * 3, 3) else "medium",
        "recommended_strategy": "build_tree_from_chunks" if graph_clearly_weak else "build_tree_from_subgraph",
        "reason": (
            "Graph skeleton has nodes and causal edges; prefer graph-guided generation."
            if not graph_clearly_weak
            else "Graph skeleton is empty or too sparse; use evidence-only chunk generation."
        ),
        "missing_context": [] if not graph_clearly_weak else ["usable_graph_edges"],
        "source": "deterministic_fallback",
    }


def _bounded_float(value: Any, fallback: Any) -> float:
    try:
        number = float(value)
    except Exception:
        try:
            number = float(fallback)
        except Exception:
            number = 0.0
    return round(max(0.0, min(1.0, number)), 3)


def _sample_graph_nodes(nodes: List[Dict[str, Any]], limit: int = 8) -> List[Dict[str, Any]]:
    return [
        {
            "id": node.get("graph_node_id") or node.get("id"),
            "name": node.get("name"),
            "type": node.get("type") or node.get("label"),
        }
        for node in nodes[:limit]
        if isinstance(node, dict)
    ]


def _sample_graph_edges(edges: List[Dict[str, Any]], limit: int = 10) -> List[Dict[str, Any]]:
    return [
        {
            "source": edge.get("source") or edge.get("source_id") or edge.get("from"),
            "target": edge.get("target") or edge.get("target_id") or edge.get("to"),
            "type": edge.get("type") or edge.get("relation"),
        }
        for edge in edges[:limit]
        if isinstance(edge, dict)
    ]


def _sample_chunks(chunks: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    sample = []
    for chunk in chunks[:limit]:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text") or chunk.get("content") or chunk.get("chunk_text") or "")
        sample.append(
            {
                "chunk_id": chunk.get("chunk_uid") or chunk.get("chunk_id"),
                "source": chunk.get("source") or chunk.get("file_name"),
                "preview": text[:180],
            }
        )
    return sample
