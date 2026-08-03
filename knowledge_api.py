from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from import_cluster_intermediate_to_kb import import_to_neo4j, import_top_event_catalog
from import_chunks import _load_chunks
from knowledge_store import (
    activate_file_version,
    archive_file,
    assert_chunk_artifacts_align_with_file_version,
    assert_source_record_artifacts_align_with_file_version,
    create_file_version_record,
    get_chunk_by_id,
    import_chunks as import_chunks_to_db,
    import_maintenance_cases as import_maintenance_cases_to_db,
    import_work_orders as import_work_orders_to_db,
    list_all_chunks,
    mark_file_version_import_failed,
)


class KnowledgeArtifactsImportRequest(BaseModel):
    chunks_file: str
    cluster_intermediate_file: Optional[str] = None
    entities_file: Optional[str] = None
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


def _load_cluster_intermediate(file_path: Path) -> Dict[str, Any]:
    if not file_path.exists():
        raise ValueError(f"cluster_intermediate_file does not exist: {file_path}")
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("cluster_intermediate_file must contain a JSON object")
    required_keys = {"mentions", "clusters", "raw_relations", "clustered_relations"}
    missing = [key for key in sorted(required_keys) if key not in data]
    if missing:
        raise ValueError(f"cluster_intermediate_file is missing required keys: {', '.join(missing)}")
    return data


def _import_cluster_intermediate_artifact(
    cluster_intermediate_file: Path,
    *,
    file_id: str,
    file_version_id: str,
    file_name: str,
    clear_scope: bool = True,
) -> Dict[str, Any]:
    data = _load_cluster_intermediate(cluster_intermediate_file)
    neo4j_result = import_to_neo4j(
        data,
        file_id=file_id,
        file_version_id=file_version_id,
        file_name=file_name,
        clear_scope=clear_scope,
    )
    catalog_result = import_top_event_catalog(
        data,
        file_id=file_id,
        file_version_id=file_version_id,
        file_name=file_name,
        clear_scope=clear_scope,
    )
    return {
        "neo4j": neo4j_result,
        "mongo": catalog_result,
        "status": "success",
    }


def import_knowledge_artifacts_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    req = KnowledgeArtifactsImportRequest(**payload)
    chunks_path = Path(req.chunks_file).expanduser().resolve()
    cluster_intermediate_path = (
        Path(req.cluster_intermediate_file).expanduser().resolve() if req.cluster_intermediate_file else None
    )

    if not chunks_path.exists():
        raise ValueError(f"chunks_file does not exist: {chunks_path}")
    if not cluster_intermediate_path:
        legacy_parts = [name for name, value in [("entities_file", req.entities_file), ("relations_file", req.relations_file)] if value]
        legacy_hint = f" Received legacy fields: {', '.join(legacy_parts)}." if legacy_parts else ""
        raise ValueError(
            "KB v2 import requires cluster_intermediate_file generated by entity_clustering/cluster_entities.py "
            "or kb_pipeline_v2.py. Legacy entities_file/relations_file graph import is no longer supported."
            + legacy_hint
        )
    if not cluster_intermediate_path.exists():
        raise ValueError(f"cluster_intermediate_file does not exist: {cluster_intermediate_path}")
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
            relations_path=None,
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
                    "cluster_intermediate_file": str(cluster_intermediate_path),
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

        graph_result = _import_cluster_intermediate_artifact(
            cluster_intermediate_path,
            file_id=file_version["file_id"],
            file_version_id=file_version["file_version_id"],
            file_name=file_version["file_name"],
            clear_scope=True,
        )

        catalog_count = int((graph_result.get("mongo") or {}).get("top_event_catalog") or 0)
        if catalog_count <= 0:
            raise ValueError("cluster_intermediate_file contains no FaultEvent clusters for top_event_catalog")
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
                "top_event_catalog": catalog_count,
            },
            "artifacts": {
                "chunks_file": str(chunks_path),
                "cluster_intermediate_file": str(cluster_intermediate_path),
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
