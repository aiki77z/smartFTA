from __future__ import annotations

import json
from typing import Any, Dict, List

from openai import OpenAI

from config import GRAPH_TREE_MAX_DEPTH, GRAPH_TREE_MAX_NODES, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PARSE_MAX_TOKENS
from database import (
    collect_subgraph_chunks,
    expand_scoped_local_fault_subgraph,
    get_chunks_by_ids,
    get_graph_node_by_id,
    match_top_event_from_graph,
    resolve_selected_file_version_ids,
)
from generator import (
    MAX_CHUNKS_FOR_PROMPT,
    _append_unique_chunks,
    _collect_chunk_only_evidence,
    _make_stage_profile,
    build_top_event_normalized_candidates,
)

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


class RetrievalAgent:
    name = "RetrievalAgent"

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()

    def run(self, state: Dict[str, Any], catalog: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(state.get("run_id") or "")
        top_event = str(state.get("resolved_top_event") or catalog.get("name") or state.get("requested_top_event") or "")
        selected_file_version_ids = list(state.get("selected_file_version_ids") or [])
        root_graph_node_id = state.get("graph_node_id") or catalog.get("graph_node_id")
        max_depth = ((state.get("options") or {}) if isinstance(state.get("options"), dict) else {}).get("max_depth")

        append_agentic_event(
            run_id,
            EVENT_AGENT_STARTED,
            agent=self.name,
            stage="retrieval",
            message="RetrievalAgent started autonomous retrieval planning.",
            progress=14,
            details={
                "top_event": top_event,
                "has_confirmed_graph_node": bool(root_graph_node_id),
                "memory": self._memory_hint(state),
            },
        )
        plan = self._select_tool_plan(
            top_event=top_event,
            has_confirmed_graph_node=bool(root_graph_node_id),
            memory_hint=self._memory_hint(state),
        )
        append_agentic_event(
            run_id,
            EVENT_TOOL_SELECTED,
            agent=self.name,
            stage="retrieval",
            message=f"RetrievalAgent selected tool plan: {', '.join(plan)}.",
            progress=15,
            details={"plan": plan},
        )

        context: Dict[str, Any] = {"performance": {}}
        try:
            if "match_graph_top_event" in plan:
                match_payload = self._call_tool(
                    run_id,
                    "match_graph_top_event",
                    "graph_match",
                    progress=20,
                    func=lambda: self._match_graph_top_event(top_event, selected_file_version_ids, root_graph_node_id),
                )
            else:
                match_payload = self._match_graph_top_event(top_event, selected_file_version_ids, root_graph_node_id)

            context.update(match_payload)
            subgraph_payload = self._call_tool(
                run_id,
                "expand_subgraph",
                "graph_subgraph",
                progress=28,
                func=lambda: self._expand_subgraph(context, selected_file_version_ids, max_depth),
            )
            context.update(subgraph_payload)
            chunk_payload = self._call_tool(
                run_id,
                "collect_chunks",
                "graph_chunks",
                progress=38,
                func=lambda: self._collect_chunks(context, selected_file_version_ids),
            )
            context.update(chunk_payload)
        except Exception as exc:
            append_tool_event(
                run_id,
                EVENT_TOOL_FAILED,
                agent=self.name,
                tool="retrieval_plan",
                stage="retrieval",
                message=f"RetrievalAgent retrieval plan failed: {exc}",
                reason=str(exc),
            )
            raise

        append_agentic_event(
            run_id,
            EVENT_AGENT_COMPLETED,
            agent=self.name,
            stage="graph_chunks",
            message="RetrievalAgent completed retrieval context.",
            progress=42,
            details={
                "matched_top_event": (context.get("matched") or {}).get("matched_name"),
                "subgraph_node_count": len((context.get("subgraph_bundle") or {}).get("nodes") or []),
                "subgraph_edge_count": len((context.get("subgraph_bundle") or {}).get("edges") or []),
                "evidence_chunk_count": len(context.get("raw_chunks") or []),
                "fallback_used": bool(context.get("fallback_used")),
            },
        )
        return context

    def _select_tool_plan(self, *, top_event: str, has_confirmed_graph_node: bool, memory_hint: Dict[str, Any]) -> List[str]:
        allowed = [tool.name for tool in self.registry.list_for_agent(self.name)]
        fallback = ["expand_subgraph", "collect_chunks"] if has_confirmed_graph_node else ["match_graph_top_event", "expand_subgraph", "collect_chunks"]
        if not LLM_API_KEY:
            return fallback
        prompt = {
            "role": "user",
            "content": (
                "You are RetrievalAgent for a fault-tree generation system. "
                "Choose a minimal ordered JSON array of tool names from the allowed list. "
                "Use match_graph_top_event only when no confirmed graph node is available. "
                f"Top event: {top_event}\n"
                f"Has confirmed graph node: {has_confirmed_graph_node}\n"
                f"Runtime memory summary: {json.dumps(memory_hint, ensure_ascii=False)}\n"
                f"Allowed tools: {allowed}\n"
                "Return only JSON, for example: [\"match_graph_top_event\", \"expand_subgraph\", \"collect_chunks\"]"
            ),
        }
        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[prompt],
                temperature=0.0,
                max_tokens=min(LLM_PARSE_MAX_TOKENS, 200),
            )
            parsed = json.loads(response.choices[0].message.content or "[]")
            plan = [str(item) for item in parsed if str(item) in allowed]
            if "expand_subgraph" in plan and "collect_chunks" in plan:
                if has_confirmed_graph_node:
                    return [item for item in plan if item != "match_graph_top_event"]
                if "match_graph_top_event" in plan:
                    return plan
        except Exception:
            return fallback
        return fallback

    def _memory_hint(self, state: Dict[str, Any]) -> Dict[str, Any]:
        memory = state.get("memory_context") if isinstance(state.get("memory_context"), dict) else {}
        return {
            "previous_artifact_types": sorted(((memory.get("artifacts") or {}).get("type_counts") or {}).keys()),
            "previous_event_count": (memory.get("events") or {}).get("event_count", 0),
            "scope_key": (memory.get("run") or {}).get("scope_key"),
        }

    def _call_tool(self, run_id: str, tool: str, stage: str, *, progress: int, func) -> Dict[str, Any]:
        self.registry.assert_allowed(self.name, tool)
        append_tool_event(
            run_id,
            EVENT_TOOL_STARTED,
            agent=self.name,
            tool=tool,
            stage=stage,
            message=f"RetrievalAgent started tool: {tool}.",
            progress=progress,
        )
        try:
            result = func()
        except Exception as exc:
            append_tool_event(
                run_id,
                EVENT_TOOL_FAILED,
                agent=self.name,
                tool=tool,
                stage=stage,
                message=f"RetrievalAgent tool failed: {tool}.",
                reason=str(exc),
            )
            raise
        append_tool_event(
            run_id,
            EVENT_TOOL_COMPLETED,
            agent=self.name,
            tool=tool,
            stage=stage,
            message=f"RetrievalAgent completed tool: {tool}.",
            progress=progress + 5,
            details=self._summarize_tool_result(tool, result),
        )
        return result

    def _match_graph_top_event(
        self,
        top_event: str,
        selected_file_version_ids: List[str],
        root_graph_node_id: Any,
    ) -> Dict[str, Any]:
        import time

        started = time.perf_counter()
        scoped_file_version_ids = resolve_selected_file_version_ids(
            selected_file_version_ids,
            fallback_to_active=True,
            require_active=False,
        )
        if root_graph_node_id:
            matched_node = get_graph_node_by_id(
                root_graph_node_id,
                selected_file_version_ids=scoped_file_version_ids,
            )
            if not matched_node:
                raise ValueError(f"Confirmed graph node not found in current scope: {root_graph_node_id}")
            matched = {
                "matched_node_id": matched_node.get("graph_node_id"),
                "matched_name": matched_node.get("name") or top_event,
                "matched_node": matched_node,
                "matched_nodes": [
                    {
                        "graph_node_id": matched_node.get("graph_node_id"),
                        "name": matched_node.get("name"),
                        "normalized_name": matched_node.get("normalized_name"),
                        "file_id": matched_node.get("file_id"),
                        "file_version_id": matched_node.get("file_version_id"),
                    }
                ],
                "alternatives": [],
            }
            root_node_ids = [matched_node.get("graph_node_id")]
        else:
            normalized_candidates = build_top_event_normalized_candidates(top_event, [top_event])
            matched = match_top_event_from_graph(
                top_event,
                normalized_candidates=normalized_candidates,
                selected_file_version_ids=scoped_file_version_ids,
            )
            root_node_ids = [
                item.get("graph_node_id")
                for item in (matched.get("matched_nodes") or [])
                if item.get("graph_node_id")
            ]
        return {
            "matched": matched,
            "root_node_ids": root_node_ids,
            "scoped_file_version_ids": scoped_file_version_ids,
            "performance": {
                "graph_match": _make_stage_profile(
                    time.perf_counter() - started,
                    matched_node_count=len(root_node_ids or ([matched.get("matched_node_id")] if matched.get("matched_node_id") else [])),
                    matched_top_event=matched.get("matched_name"),
                    used_confirmed_graph_node=bool(root_graph_node_id),
                )
            },
        }

    def _expand_subgraph(self, context: Dict[str, Any], selected_file_version_ids: List[str], max_depth: Any) -> Dict[str, Any]:
        import time

        started = time.perf_counter()
        matched = context["matched"]
        scoped_file_version_ids = context.get("scoped_file_version_ids") or selected_file_version_ids
        effective_max_depth = GRAPH_TREE_MAX_DEPTH if max_depth is None else max(1, min(int(max_depth), 10))
        subgraph_bundle = expand_scoped_local_fault_subgraph(
            context.get("root_node_ids") or [matched["matched_node_id"]],
            max_depth=effective_max_depth,
            max_nodes=GRAPH_TREE_MAX_NODES,
            selected_file_version_ids=scoped_file_version_ids,
        )
        return {
            "subgraph_bundle": subgraph_bundle,
            "performance": {
                **(context.get("performance") or {}),
                "graph_subgraph": _make_stage_profile(
                    time.perf_counter() - started,
                    root_count=len(subgraph_bundle.get("roots") or []),
                    node_count=len(subgraph_bundle.get("nodes") or []),
                    edge_count=len(subgraph_bundle.get("edges") or []),
                    gate_group_count=len(subgraph_bundle.get("gate_groups") or []),
                    pruned_transitive_edge_count=len(subgraph_bundle.get("pruned_transitive_edges") or []),
                    multi_root_runtime_merge=bool(len(subgraph_bundle.get("roots") or []) > 1),
                ),
            },
        }

    def _collect_chunks(self, context: Dict[str, Any], selected_file_version_ids: List[str]) -> Dict[str, Any]:
        import time

        started = time.perf_counter()
        matched = context["matched"]
        scoped_file_version_ids = context.get("scoped_file_version_ids") or selected_file_version_ids
        subgraph_bundle = context["subgraph_bundle"]
        chunk_ids = list(collect_subgraph_chunks(subgraph_bundle, chunk_limit=MAX_CHUNKS_FOR_PROMPT))
        raw_chunks: List[Dict[str, Any]] = []
        if chunk_ids:
            raw_chunks = get_chunks_by_ids(
                chunk_ids,
                limit=max(len(chunk_ids), 1),
                selected_file_version_ids=scoped_file_version_ids,
            )
        fallback_used = False
        if not raw_chunks:
            fallback_used = True
            fallback_retrieval = _collect_chunk_only_evidence(
                matched["matched_name"],
                selected_file_version_ids=scoped_file_version_ids,
                limit=MAX_CHUNKS_FOR_PROMPT,
            )
            _append_unique_chunks(raw_chunks, fallback_retrieval.get("chunks") or [])
            for chunk_id in fallback_retrieval.get("chunk_ids") or []:
                if chunk_id not in chunk_ids:
                    chunk_ids.append(chunk_id)
        if not raw_chunks:
            raise ValueError(f"No evidence chunks found for '{matched['matched_name']}'")
        return {
            "chunk_ids": chunk_ids,
            "raw_chunks": raw_chunks,
            "fallback_used": fallback_used,
            "performance": {
                **(context.get("performance") or {}),
                "chunk_recall": _make_stage_profile(
                    time.perf_counter() - started,
                    recalled_chunk_count=len(chunk_ids),
                    retained_chunk_count=len(raw_chunks),
                    source_file_version_count=len({chunk.get("file_version_id") for chunk in raw_chunks if chunk.get("file_version_id")}),
                    fallback_used=fallback_used,
                ),
            },
        }

    def _summarize_tool_result(self, tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
        if tool == "match_graph_top_event":
            matched = result.get("matched") or {}
            return {
                "matched_top_event": matched.get("matched_name"),
                "matched_node_id": matched.get("matched_node_id"),
                "matched_node_count": len(result.get("root_node_ids") or []),
            }
        if tool == "expand_subgraph":
            subgraph = result.get("subgraph_bundle") or {}
            return {
                "subgraph_node_count": len(subgraph.get("nodes") or []),
                "subgraph_edge_count": len(subgraph.get("edges") or []),
                "gate_group_count": len(subgraph.get("gate_groups") or []),
            }
        if tool == "collect_chunks":
            return {
                "chunk_count": len(result.get("raw_chunks") or []),
                "chunk_ids": result.get("chunk_ids") or [],
                "fallback_used": bool(result.get("fallback_used")),
            }
        return {}
