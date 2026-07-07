import json
import urllib.error
import urllib.request
import urllib.parse
from typing import Any, Dict, List, Optional

from app.config import FTA_GNR_AGENT_RUN_PATH, FTA_GNR_BASE_URL


class GnrClient:
    def __init__(self, base_url: str = FTA_GNR_BASE_URL):
        self.base_url = base_url.rstrip("/")

    def generate_tree(
        self,
        *,
        prompt: str,
        selected_file_version_ids: List[str],
        sync: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "prompt": prompt,
            "sync": sync,
        }
        ids = [str(x).strip() for x in selected_file_version_ids if str(x).strip()]
        if ids:
            body["selected_file_version_ids"] = ids
        if extra:
            body.update(extra)
        return self._post_json("/api/tree/generate", body)

    def run_generation_agent(
        self,
        *,
        prompt: str,
        selected_file_version_ids: List[str],
        session_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if FTA_GNR_AGENT_RUN_PATH:
            body: Dict[str, Any] = {
                "task_type": "generate_fault_tree",
                "prompt": prompt,
                "selected_file_version_ids": selected_file_version_ids,
            }
            if session_id:
                body["session_id"] = session_id
            if extra:
                body.update(extra)
            return self._post_json(FTA_GNR_AGENT_RUN_PATH, body)
        return self.generate_tree(
            prompt=prompt,
            selected_file_version_ids=selected_file_version_ids,
            sync=False,
            extra=extra,
        )

    def validate_tree(self, *, tree_data: Any) -> Dict[str, Any]:
        return self._post_json("/api/tree/validate", {"tree_data": tree_data})

    def list_catalog_top_events(self, *, selected_file_version_ids: List[str]) -> Dict[str, Any]:
        ids = ",".join([str(x).strip() for x in selected_file_version_ids if str(x).strip()])
        return self._get_json("/api/catalog/top-events", {"selected_file_version_ids": ids} if ids else {})

    def list_graph_top_events(self, *, selected_file_version_ids: List[str], limit: int = 50) -> Dict[str, Any]:
        ids = ",".join([str(x).strip() for x in selected_file_version_ids if str(x).strip()])
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or 50), 200))}
        if ids:
            params["selected_file_version_ids"] = ids
        return self._get_json("/api/top-events", params)

    def list_chunks(
        self,
        *,
        file_version_ids: List[str],
        file_names: Optional[List[str]] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or 20), 100))}
        ids = [str(x).strip() for x in file_version_ids if str(x).strip()]
        names = [str(x).strip() for x in (file_names or []) if str(x).strip()]
        if ids:
            params["file_version_ids"] = ids
        if names:
            params["file_names"] = names
        return self._get_json("/api/chunks", params, doseq=True)

    def get_chunk(self, *, chunk_id: str) -> Dict[str, Any]:
        chunk_id = str(chunk_id or "").strip()
        if not chunk_id:
            raise ValueError("chunk_id is required")
        return self._get_json(f"/api/chunk/{urllib.parse.quote(chunk_id)}")

    def graph_cypher_query(self, *, cypher: str, params: Optional[Dict[str, Any]] = None, limit: int = 50) -> Dict[str, Any]:
        return self._post_json("/api/graph/cypher", {
            "cypher": cypher,
            "params": params or {},
            "limit": max(1, min(int(limit or 50), 200)),
        })

    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None, doseq: bool = False) -> Dict[str, Any]:
        query = urllib.parse.urlencode(params or {}, doseq=doseq)
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        if query:
            url = f"{url}?{query}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GNR request failed ({e.code}): {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"GNR service unavailable: {e.reason}") from e

    def _post_json(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path if path.startswith('/') else '/' + path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GNR request failed ({e.code}): {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"GNR service unavailable: {e.reason}") from e


gnr_client = GnrClient()
