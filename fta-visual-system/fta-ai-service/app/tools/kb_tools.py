from typing import Any, Dict, List, Optional

from app.tools.gnr_client import gnr_client
from app.services.tree_utils import normalize_to_graph


class KnowledgeReaderTool:
    """Small read-only facade over FTA-GNR knowledge endpoints.

    The assistant uses this for dialogue support and intent clarification only.
    It never writes to MongoDB/Neo4j and always keeps returned text bounded.
    """

    def top_event_candidates(self, selected_file_version_ids: List[str], limit: int = 12) -> Dict[str, Any]:
        ids = self._ids(selected_file_version_ids)
        if not ids:
            return {"items": [], "total": 0, "scope": [], "note": "no selected file_version_ids"}
        data = gnr_client.list_catalog_top_events(selected_file_version_ids=ids)
        items = data.get("items") if isinstance(data, dict) else []
        compact = [self._compact_top_event(item) for item in (items or [])[: max(1, min(limit, 30))]]
        return {
            "items": compact,
            "total": int(data.get("total") or len(items or [])) if isinstance(data, dict) else len(compact),
            "scope": data.get("selected_file_version_ids", ids) if isinstance(data, dict) else ids,
            "source": data.get("discovery_source") if isinstance(data, dict) else "",
        }

    def chunks_preview(
        self,
        selected_file_version_ids: List[str],
        *,
        file_names: Optional[List[str]] = None,
        limit: int = 8,
    ) -> Dict[str, Any]:
        ids = self._ids(selected_file_version_ids)
        if not ids and not file_names:
            return {"chunks": [], "total": 0, "scope": [], "note": "no selected files"}
        data = gnr_client.list_chunks(file_version_ids=ids, file_names=file_names or [], limit=limit)
        chunks = data.get("chunks") if isinstance(data, dict) else []
        return {
            "chunks": [self._compact_chunk(c) for c in (chunks or [])[: max(1, min(limit, 20))]],
            "total": int(data.get("total") or len(chunks or [])) if isinstance(data, dict) else 0,
            "scope": ids,
        }

    def chunk_detail(self, chunk_id: str, max_text_chars: int = 1200) -> Dict[str, Any]:
        data = gnr_client.get_chunk(chunk_id=chunk_id)
        compact = self._compact_chunk(data, max_text_chars=max_text_chars)
        compact["chunk_id"] = chunk_id
        return compact

    def readonly_graph(self, cypher: str, params: Optional[Dict[str, Any]] = None, limit: int = 50) -> Dict[str, Any]:
        return gnr_client.graph_cypher_query(cypher=cypher, params=params or {}, limit=limit)

    def node_evidence(
        self,
        *,
        tree_json: Any,
        node_query: str,
        selected_file_version_ids: List[str],
        max_chunks: int = 5,
    ) -> Dict[str, Any]:
        node = self._find_tree_node(tree_json, node_query)
        if not node:
            return {
                "node": None,
                "chunks": [],
                "matched_by": "",
                "note": "node not found in current tree",
            }

        chunk_ids = self._extract_chunk_ids(node.get("raw") or node)
        chunks: List[Dict[str, Any]] = []
        errors: List[str] = []
        graph_context: Dict[str, Any] = {}
        query_terms = self._query_terms(str(node.get("label") or node_query or ""))

        if not chunk_ids:
            graph_context = self.graph_neighbors(query_terms, selected_file_version_ids, limit=20)
            chunk_ids = self._extract_graph_chunk_ids(graph_context)

        for chunk_id in chunk_ids[: max(1, min(max_chunks, 10))]:
            try:
                chunks.append(self.chunk_detail(chunk_id, max_text_chars=1200))
            except Exception as exc:
                errors.append(f"{chunk_id}: {exc}")

        matched_by = "node_chunk_ids" if self._extract_chunk_ids(node.get("raw") or node) else "graph_source_chunk_ids"
        if not chunks:
            preview = self.chunks_preview(selected_file_version_ids, limit=20)
            node_label = str(node.get("label") or "")
            chunks = self._rank_chunks_by_text(" ".join(query_terms or [node_label]), preview.get("chunks") or [], limit=max_chunks)
            matched_by = "node_label_chunk_preview" if chunks else matched_by

        if not graph_context:
            graph_context = self.graph_neighbors(query_terms, selected_file_version_ids, limit=12)
        return {
            "node": {
                "id": node.get("id"),
                "label": node.get("label"),
                "type": node.get("type"),
            },
            "chunks": chunks[:max_chunks],
            "chunk_ids": chunk_ids,
            "matched_by": matched_by,
            "graph_context": graph_context,
            "errors": errors,
        }

    def graph_neighbors(
        self,
        query_text: Any,
        selected_file_version_ids: List[str],
        limit: int = 20,
    ) -> Dict[str, Any]:
        terms = self._query_terms(query_text)
        if not terms:
            return {"nodes": [], "edges": [], "note": "empty graph query"}
        cypher = (
            "MATCH p=(n)-[r]-(m) "
            "WHERE any(q IN $queries WHERE "
            "  coalesce(n.name, '') CONTAINS q OR coalesce(n.label, '') CONTAINS q OR "
            "  coalesce(n.title, '') CONTAINS q OR coalesce(n.entity_name, '') CONTAINS q OR "
            "  coalesce(n.description, '') CONTAINS q OR coalesce(n.id, '') CONTAINS q OR "
            "  coalesce(n.part_id, '') CONTAINS q OR coalesce(n.code, '') CONTAINS q OR "
            "  coalesce(m.name, '') CONTAINS q OR coalesce(m.label, '') CONTAINS q OR "
            "  coalesce(m.title, '') CONTAINS q OR coalesce(m.entity_name, '') CONTAINS q OR "
            "  coalesce(m.description, '') CONTAINS q OR coalesce(m.id, '') CONTAINS q OR "
            "  coalesce(m.part_id, '') CONTAINS q OR coalesce(m.code, '') CONTAINS q"
            ") "
            "AND (size($file_version_ids) = 0 OR "
            "  coalesce(n.file_version_id, '') IN $file_version_ids OR "
            "  coalesce(m.file_version_id, '') IN $file_version_ids OR "
            "  coalesce(r.file_version_id, '') IN $file_version_ids"
            ") "
            "RETURN p"
        )
        params: Dict[str, Any] = {"queries": terms[:8], "file_version_ids": self._ids(selected_file_version_ids)}
        try:
            data = self.readonly_graph(cypher, params=params, limit=limit)
        except Exception as exc:
            return {"nodes": [], "edges": [], "error": str(exc)}
        return self._compact_graph(data)

    def graph_question(
        self,
        *,
        question: str,
        selected_file_version_ids: List[str],
        limit: int = 30,
    ) -> Dict[str, Any]:
        text = str(question or "").strip()
        if not text:
            return {"nodes": [], "edges": [], "note": "empty graph question"}
        return self.graph_neighbors(text[:80], selected_file_version_ids, limit=limit)

    def _ids(self, values: List[str]) -> List[str]:
        out: List[str] = []
        for value in values or []:
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
        return out

    def _compact_top_event(self, item: Any) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {"name": str(item)[:120]}
        return {
            "name": self._first_text(item, ["name", "top_event", "event", "title", "label"], 160),
            "description": self._first_text(item, ["description", "summary", "evidence", "reason"], 260),
            "score": item.get("score") or item.get("confidence") or item.get("weight"),
            "source": self._first_text(item, ["source", "file", "doc_name", "file_name"], 120),
        }

    def _compact_chunk(self, chunk: Any, max_text_chars: int = 700) -> Dict[str, Any]:
        if not isinstance(chunk, dict):
            return {"text": str(chunk)[:max_text_chars]}
        return {
            "id": self._first_text(chunk, ["id", "chunk_id", "_id"], 120),
            "title": self._first_text(chunk, ["chunk_name", "title", "section_path", "heading"], 160),
            "source": self._first_text(chunk, ["doc_name", "file", "source", "path"], 180),
            "text": self._first_text(chunk, ["text", "content", "cleaned_text", "chunk_text"], max_text_chars),
        }

    def _find_tree_node(self, tree_json: Any, node_query: str) -> Optional[Dict[str, Any]]:
        nodes, _ = normalize_to_graph(tree_json)
        raw_nodes = self._raw_nodes(tree_json)
        raw_by_id = {
            str(n.get("id") or n.get("event_id") or n.get("node_id") or ""): n
            for n in raw_nodes
            if isinstance(n, dict)
        }
        query = str(node_query or "").strip()
        if not query and nodes:
            item = nodes[0]
            return {**item, "raw": raw_by_id.get(str(item.get("id") or ""), item)}
        for item in nodes:
            if query and str(item.get("id") or "") == query:
                return {**item, "raw": raw_by_id.get(str(item.get("id") or ""), item)}
        for item in nodes:
            label = str(item.get("label") or "")
            if query and label == query:
                return {**item, "raw": raw_by_id.get(str(item.get("id") or ""), item)}
        matches = []
        for item in nodes:
            label = str(item.get("label") or "")
            if query and (query in label or label in query):
                matches.append(item)
        if len(matches) == 1:
            item = matches[0]
            return {**item, "raw": raw_by_id.get(str(item.get("id") or ""), item)}
        return None

    def _raw_nodes(self, tree_json: Any) -> List[Dict[str, Any]]:
        if isinstance(tree_json, dict) and isinstance(tree_json.get("tree_data"), dict):
            tree_json = tree_json["tree_data"]
        if isinstance(tree_json, dict) and isinstance(tree_json.get("nodeList"), list):
            return [n for n in tree_json["nodeList"] if isinstance(n, dict)]
        if isinstance(tree_json, dict) and isinstance(tree_json.get("nodes"), list):
            return [n for n in tree_json["nodes"] if isinstance(n, dict)]
        return []

    def _extract_chunk_ids(self, node: Dict[str, Any]) -> List[str]:
        keys = [
            "evidence_chunk_ids",
            "source_chunk_ids",
            "chunk_ids",
            "chunks",
            "evidenceChunks",
            "sourceChunks",
        ]
        out: List[str] = []
        for key in keys:
            value = node.get(key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict):
                    item = item.get("id") or item.get("chunk_id")
                text = str(item or "").strip()
                if text and text not in out:
                    out.append(text)
        return out

    def _rank_chunks_by_text(self, needle: str, chunks: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        tokens = [t for t in str(needle or "").replace("/", " ").replace("-", " ").split() if t]
        scored = []
        for chunk in chunks:
            blob = f"{chunk.get('title', '')} {chunk.get('source', '')} {chunk.get('text', '')}"
            score = 0
            if needle and needle in blob:
                score += 10
            score += sum(1 for token in tokens if token in blob)
            if score:
                scored.append((score, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [chunk for _, chunk in scored[: max(1, min(limit, 10))]]

    def _query_terms(self, query_text: Any) -> List[str]:
        if isinstance(query_text, list):
            raw_parts = [str(x or "").strip() for x in query_text]
        else:
            raw_parts = [str(query_text or "").strip()]
        terms: List[str] = []

        def add(value: str) -> None:
            text = value.strip().strip("“”\"'`，。？！；：:;,.!?")
            if text and text not in terms:
                terms.append(text)

        for raw in raw_parts:
            if not raw:
                continue
            add(raw)
            compact = raw
            for word in ("当前", "画布", "故障树", "知识库", "来源依据", "来源", "依据", "节点", "事件", "是什么", "什么"):
                compact = compact.replace(word, "")
            add(compact)
            for suffix in ("失效", "故障", "异常", "损坏", "无响应"):
                if compact.endswith(suffix):
                    add(compact[: -len(suffix)])
                add(compact.replace(suffix, ""))
            if "（" in compact and "）" in compact:
                inside = compact.split("（", 1)[1].split("）", 1)[0]
                before = compact.split("（", 1)[0]
                add(before)
                add(f"{before}（{inside}）")
                add(f"{before} ({inside})")
            if "(" in compact and ")" in compact:
                inside = compact.split("(", 1)[1].split(")", 1)[0]
                before = compact.split("(", 1)[0]
                add(before)
                add(f"{before}({inside})")
            upper = compact.upper()
            if "功能按键" in compact and ("黑" in compact or "BLACK" in upper):
                add("功能按键（黑）")
                add("功能按键")
                add("BUTTON_BLACK")
                add("黑色功能按键")
        return terms[:12]

    def _extract_graph_chunk_ids(self, graph_context: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        for group in ("nodes", "edges"):
            for item in graph_context.get(group) or []:
                if not isinstance(item, dict):
                    continue
                for key in ("source_chunk_ids", "source_chunk_refs", "chunk_ids", "documents"):
                    value = item.get(key)
                    values = value if isinstance(value, list) else [value]
                    for entry in values:
                        if isinstance(entry, dict):
                            entry = entry.get("chunk_uid") or entry.get("chunk_id") or entry.get("id")
                        text = str(entry or "").strip()
                        if text and text not in out:
                            out.append(text)
        return out

    def _compact_graph(self, data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            return {"raw": str(data)[:1000]}
        graph = data.get("graph") if isinstance(data.get("graph"), dict) else data
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
        return {
            "nodes": [self._compact_graph_node(n) for n in nodes[:30]],
            "edges": [self._compact_graph_edge(e) for e in edges[:40]],
            "rows_scanned": (data.get("stats") or {}).get("rows_scanned") if isinstance(data.get("stats"), dict) else data.get("row_count"),
            "node_count": graph.get("node_count") or len(nodes),
            "edge_count": graph.get("edge_count") or len(edges),
        }

    def _compact_graph_node(self, node: Any) -> Dict[str, Any]:
        if not isinstance(node, dict):
            return {"label": str(node)[:160]}
        props = node.get("props") if isinstance(node.get("props"), dict) else node.get("properties") if isinstance(node.get("properties"), dict) else node
        labels = node.get("labels") or node.get("label") or []
        return {
            "id": str(node.get("graph_node_id") or node.get("id") or props.get("id") or "")[:80],
            "labels": labels,
            "name": self._first_text(props, ["name", "label", "title", "event", "top_event"], 160),
            "type": self._first_text(props, ["type", "event_type", "category"], 80),
            "source_chunk_ids": self._list_values(props.get("source_chunk_ids")),
            "source_chunk_refs": self._list_values(props.get("source_chunk_refs")),
            "chunk_ids": self._list_values(props.get("chunk_ids")),
            "documents": self._list_values(props.get("documents")),
        }

    def _compact_graph_edge(self, edge: Any) -> Dict[str, Any]:
        if not isinstance(edge, dict):
            return {"type": str(edge)[:120]}
        return {
            "source": str(edge.get("source_graph_node_id") or edge.get("source") or edge.get("start") or "")[:80],
            "target": str(edge.get("target_graph_node_id") or edge.get("target") or edge.get("end") or "")[:80],
            "type": str(edge.get("rel_type") or edge.get("type") or edge.get("label") or "")[:100],
            "source_chunk_ids": self._list_values(props.get("source_chunk_ids")) if isinstance((props := edge.get("rel_props") or edge.get("properties") or edge), dict) else [],
            "source_chunk_refs": self._list_values(props.get("source_chunk_refs")) if isinstance(props, dict) else [],
            "chunk_ids": self._list_values(props.get("chunk_ids")) if isinstance(props, dict) else [],
            "documents": self._list_values(props.get("documents")) if isinstance(props, dict) else [],
        }

    def _list_values(self, value: Any, limit: int = 20) -> List[Any]:
        if value in (None, ""):
            return []
        values = value if isinstance(value, list) else [value]
        out: List[Any] = []
        for item in values:
            if item not in (None, "") and item not in out:
                out.append(item)
        return out[:limit]

    def _first_text(self, obj: Dict[str, Any], keys: List[str], limit: int) -> str:
        for key in keys:
            value = obj.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text[:limit]
        return ""


knowledge_reader_tool = KnowledgeReaderTool()
