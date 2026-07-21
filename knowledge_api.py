from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from config import NEO4J_DATABASE, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from import_chunks import _load_chunks
from import_relations_to_neo4j import (
    GraphDatabase,
    ensure_constraints,
    import_entities,
    import_rows,
    load_entities,
    load_json,
)
from knowledge_store import (
    activate_file_version,
    archive_file,
    assert_chunk_artifacts_align_with_file_version,
    assert_relation_artifacts_align_with_file_version,
    assert_source_record_artifacts_align_with_file_version,
    create_file_version_record,
    get_chunk_by_id,
    import_chunks as import_chunks_to_db,
    import_maintenance_cases as import_maintenance_cases_to_db,
    import_work_orders as import_work_orders_to_db,
    list_all_chunks,
    mark_file_version_import_failed,
    rebuild_top_event_catalog_for_file_version,
)


class KnowledgeArtifactsImportRequest(BaseModel):
    chunks_file: str
    entities_file: str
    relations_file: Optional[str] = None
    clear_graph: bool = False
    source: str = "knowledge_base_construction"
    file_id: Optional[str] = None
    file_name: Optional[str] = None
    file_version_id: Optional[str] = None
    chunks_import_mode: str = "replace"
    import_relations: bool = True


def _derive_file_name_from_artifacts(
    *,
    explicit_file_name: Optional[str] = None,
    chunks: Optional[List[Dict[str, Any]]] = None,
    chunks_path: Optional[Path] = None,
    relations_path: Optional[Path] = None,
) -> str:
    explicit = str(explicit_file_name or "").strip()
    if explicit:
        return explicit
    first_chunk = (chunks or [{}])[0] if chunks else {}
    chunk_file = str(first_chunk.get("file") or "").strip()
    if chunk_file:
        return chunk_file
    if relations_path:
        relation_name = relations_path.name
        if relation_name.endswith("_relations.jsonl"):
            return relation_name.replace("_relations.jsonl", "_cleaned.md")
        if relation_name.endswith("_relations.json"):
            return relation_name.replace("_relations.json", "_cleaned.md")
    if chunks_path:
        return chunks_path.stem + ".md"
    return "knowledge_artifacts.md"


def _derive_file_identity_from_chunks(
    chunks: Optional[List[Dict[str, Any]]],
    explicit_file_id: Optional[str],
    explicit_file_version_id: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    if explicit_file_id and explicit_file_version_id:
        return explicit_file_id, explicit_file_version_id
    first_chunk = (chunks or [{}])[0] if chunks else {}
    chunk_file_id = str(first_chunk.get("file_id") or "").strip() or None
    chunk_file_version_id = str(first_chunk.get("file_version_id") or "").strip() or None
    return explicit_file_id or chunk_file_id, explicit_file_version_id or chunk_file_version_id


def _load_json_documents(file_path: Path) -> List[Dict[str, Any]]:
    if not file_path.exists():
        return []
    with file_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _discover_sidecar_record_file(chunks_path: Path, chunks: List[Dict[str, Any]], suffix: str) -> Optional[Path]:
    first_chunk = (chunks or [{}])[0] if chunks else {}
    file_id = str(first_chunk.get("file_id") or "").strip()
    if not file_id:
        stem = chunks_path.stem
        if stem.endswith("_chunks"):
            file_id = stem[: -len("_chunks")]
    if not file_id:
        return None
    candidate = chunks_path.with_name(f"{file_id}{suffix}")
    return candidate if candidate.exists() else None


def _expected_source_record_type(records: List[Dict[str, Any]], fallback: str) -> str:
    for record in records or []:
        if not isinstance(record, dict):
            continue
        value = str(record.get("source_record_type") or "").strip()
        if value:
            return value
    return fallback


def _import_graph_artifacts(
    entities_file: Path,
    relations_file: Optional[Path],
    *,
    file_id: str,
    file_version_id: str,
    file_name: str,
    import_relations: bool,
) -> Dict[str, Any]:
    entity_rows = load_entities(
        entities_file,
        file_id=file_id,
        file_version_id=file_version_id,
        is_active=True,
    )
    if not entity_rows:
        raise ValueError(f"No valid entities were extracted: {entities_file}")

    relation_rows = []
    if import_relations and relations_file:
        relation_rows = load_json(
            relations_file,
            file_id=file_id,
            file_version_id=file_version_id,
            file_name=file_name,
            is_active=True,
        )
        assert_relation_artifacts_align_with_file_version(
            relation_rows,
            file_id=file_id,
            file_version_id=file_version_id,
        )

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        driver.verify_connectivity()
        ensure_constraints(driver, NEO4J_DATABASE)
        entity_count = import_entities(driver, NEO4J_DATABASE, entity_rows)
        relation_count = import_rows(driver, NEO4J_DATABASE, relation_rows, batch_size=200) if relation_rows else 0
    finally:
        driver.close()
    return {
        "entities": entity_count,
        "relations": relation_count,
        "relation_rows": len(relation_rows),
        "status": "success",
    }


def import_knowledge_artifacts_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    req = KnowledgeArtifactsImportRequest(**payload)
    chunks_path = Path(req.chunks_file).expanduser().resolve()
    entities_path = Path(req.entities_file).expanduser().resolve() if req.entities_file else None
    relations_path = Path(req.relations_file).expanduser().resolve() if req.relations_file else None

    if not chunks_path.exists():
        raise ValueError(f"chunks_file does not exist: {chunks_path}")
    if not entities_path or not entities_path.exists():
        raise ValueError(f"entities_file does not exist: {entities_path}")
    if req.import_relations and relations_path and not relations_path.exists():
        raise ValueError(f"relations_file does not exist: {relations_path}")
    if req.clear_graph:
        raise ValueError("Versioned KB mode forbids clear_graph=true. Archive old file versions instead.")

    chunks_import_mode = str(req.chunks_import_mode or "replace").strip().lower()
    if chunks_import_mode not in {"replace", "append"}:
        raise ValueError("chunks_import_mode must be replace or append")

    file_version = None
    try:
        chunks = _load_chunks(str(chunks_path))
        file_name = _derive_file_name_from_artifacts(
            explicit_file_name=req.file_name,
            chunks=chunks,
            chunks_path=chunks_path,
            relations_path=relations_path,
        )
        resolved_file_id, resolved_file_version_id = _derive_file_identity_from_chunks(
            chunks,
            explicit_file_id=req.file_id,
            explicit_file_version_id=req.file_version_id,
        )
        chunk_source_types = {str(item.get("source_type") or "").strip() for item in (chunks or []) if isinstance(item, dict)}
        auto_work_orders_path = _discover_sidecar_record_file(chunks_path, chunks, "_work_orders.json")
        auto_maintenance_cases_path = _discover_sidecar_record_file(chunks_path, chunks, "_maintenance_cases.json")

        file_version = create_file_version_record(
            file_id=resolved_file_id,
            file_name=file_name,
            file_version_id=resolved_file_version_id,
            source=str(req.source or "knowledge_base_construction"),
            metadata={
                "artifacts": {
                    "chunks_file": str(chunks_path),
                    "entities_file": str(entities_path) if entities_path else None,
                    "relations_file": str(relations_path) if relations_path else None,
                }
            },
        )
        assert_chunk_artifacts_align_with_file_version(
            chunks,
            file_id=file_version["file_id"],
            file_version_id=file_version["file_version_id"],
        )
        chunk_import_result = import_chunks_to_db(
            chunks,
            mode=chunks_import_mode,
            file_id=file_version["file_id"],
            file_version_id=file_version["file_version_id"],
            is_active=True,
        )

        work_order_count = None
        if auto_work_orders_path and "work_order" in chunk_source_types:
            work_orders = _load_json_documents(auto_work_orders_path)
            assert_source_record_artifacts_align_with_file_version(
                work_orders,
                file_id=file_version["file_id"],
                file_version_id=file_version["file_version_id"],
                expected_source_record_type=_expected_source_record_type(work_orders, "work_order"),
            )
            work_order_count = import_work_orders_to_db(
                work_orders,
                file_id=file_version["file_id"],
                file_version_id=file_version["file_version_id"],
                is_active=True,
            )

        maintenance_case_count = None
        if auto_maintenance_cases_path and "maintenance_record" in chunk_source_types:
            cases = _load_json_documents(auto_maintenance_cases_path)
            assert_source_record_artifacts_align_with_file_version(
                cases,
                file_id=file_version["file_id"],
                file_version_id=file_version["file_version_id"],
                expected_source_record_type=_expected_source_record_type(cases, "maintenance_case"),
            )
            maintenance_case_count = import_maintenance_cases_to_db(
                cases,
                file_id=file_version["file_id"],
                file_version_id=file_version["file_version_id"],
                is_active=True,
            )

        graph_result = _import_graph_artifacts(
            entities_path,
            relations_path,
            file_id=file_version["file_id"],
            file_version_id=file_version["file_version_id"],
            file_name=file_version["file_name"],
            import_relations=req.import_relations,
        )

        catalog_entries = rebuild_top_event_catalog_for_file_version(file_version["file_version_id"])
        if not catalog_entries:
            raise ValueError("Neo4j contains no FaultPhenomenon entities for the imported file version")
        activate_file_version(file_version["file_id"], file_version["file_version_id"])

        return {
            "status": "success",
            "file": {
                "file_id": file_version["file_id"],
                "file_name": file_version["file_name"],
                "file_version_id": file_version["file_version_id"],
                "version_no": file_version["version_no"],
            },
            "imported": {
                "chunks": chunk_import_result,
                "work_orders": work_order_count,
                "maintenance_cases": maintenance_case_count,
                "graph": graph_result,
                "top_event_catalog": len(catalog_entries),
            },
            "artifacts": {
                "chunks_file": str(chunks_path),
                "entities_file": str(entities_path) if entities_path else None,
                "relations_file": str(relations_path) if relations_path else None,
            },
            "next": {
                "preview_top_events": "http://localhost:8000/api/batch/preview-top-events",
                "generate_all": "http://localhost:8000/api/batch/generate-all",
            },
        }
    except Exception as exc:
        if file_version:
            try:
                mark_file_version_import_failed(file_version["file_version_id"], str(exc))
            except Exception:
                pass
        raise


def register_knowledge_routes(app: FastAPI) -> None:
    @app.post("/api/integration/import-knowledge-artifacts")
    def api_import_knowledge_artifacts(req: KnowledgeArtifactsImportRequest):
        try:
            return import_knowledge_artifacts_payload(req.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"import knowledge artifacts failed: {exc}") from exc

    @app.get("/api/chunk/{chunk_id}")
    def api_get_chunk(chunk_id: str):
        chunk = get_chunk_by_id(chunk_id)
        if not chunk:
            raise HTTPException(status_code=404, detail=f"chunk {chunk_id} does not exist")
        chunk.pop("_id", None)
        return chunk

    @app.get("/api/chunks")
    def api_list_chunks(
        file_names: Optional[List[str]] = None,
        file_version_ids: Optional[List[str]] = None,
        all: bool = False,
        limit: int = 200,
    ):
        safe_limit = max(1, min(int(limit or 200), 1000))
        names = [str(n or "").strip() for n in (file_names or []) if str(n or "").strip()]
        fvs = [str(v or "").strip() for v in (file_version_ids or []) if str(v or "").strip()]
        if bool(all):
            chunks = list_all_chunks(selected_file_version_ids=None)
            return {"chunks": chunks[:safe_limit], "total": len(chunks)}
        if not names and not fvs:
            return {"chunks": [], "total": 0}
        chunks = list_all_chunks(selected_file_version_ids=fvs or None)
        if not names:
            return {"chunks": chunks[:safe_limit], "total": len(chunks)}

        lowered = [n.replace("\\", "/").split("/")[-1].lower() for n in names]

        def _hit(doc: Dict[str, Any]) -> bool:
            blob = " ".join(
                [
                    str(doc.get("file") or ""),
                    str(doc.get("source") or ""),
                    str(doc.get("path") or ""),
                    str(doc.get("doc_name") or ""),
                    str(doc.get("chunk_name") or ""),
                    str(doc.get("section_path") or ""),
                ]
            ).lower()
            return any(b and b in blob for b in lowered)

        filtered = [c for c in chunks if _hit(c)]
        return {"chunks": filtered[:safe_limit], "total": len(filtered)}

    @app.post("/api/files/{file_id}/archive")
    def api_archive_file(file_id: str):
        try:
            doc = archive_file(file_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not doc:
            raise HTTPException(status_code=404, detail="file does not exist")
        return {"success": True, "file": doc}