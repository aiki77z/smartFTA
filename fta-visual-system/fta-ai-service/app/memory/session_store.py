import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import DATA_DIR


class JsonSessionStore:
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or DATA_DIR
        self.sessions_path = self.data_dir / "assistant_sessions.json"
        self.legacy_messages_path = self.data_dir / "assistant_messages.jsonl"
        self.messages_dir = self.data_dir / "messages"
        self.pending_path = self.data_dir / "assistant_pending_actions.json"
        self._lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.messages_dir.mkdir(parents=True, exist_ok=True)

    def make_session_id(self, project_id: Optional[str], canvas_id: Optional[str]) -> str:
        if project_id and canvas_id:
            return f"{project_id}:canvas:{canvas_id}"
        if project_id:
            return f"{project_id}:session:{uuid.uuid4().hex[:10]}"
        return f"session:{uuid.uuid4().hex[:12]}"

    def get_session(self, session_id: str) -> Dict[str, Any]:
        sessions = self._read_json_obj(self.sessions_path)
        return dict(sessions.get(session_id) or {})

    def upsert_session(self, session_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        now = int(time.time())
        with self._lock:
            sessions = self._read_json_obj(self.sessions_path)
            current = dict(sessions.get(session_id) or {})
            current.update(patch)
            current.setdefault("session_id", session_id)
            current.setdefault("created_at", now)
            current["updated_at"] = now
            sessions[session_id] = current
            self._write_json_obj(self.sessions_path, sessions)
            return current

    def append_message(self, session_id: str, role: str, content: str, meta: Optional[Dict[str, Any]] = None) -> None:
        row = {
            "session_id": session_id,
            "role": role,
            "content": content,
            "meta": meta or {},
            "ts": int(time.time()),
        }
        with self._lock:
            with self._messages_path_for_session(session_id).open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def truncate_messages(
        self,
        session_id: str,
        *,
        frontend_message_id: Optional[str] = None,
        keep_before_index: Optional[int] = None,
    ) -> Dict[str, int]:
        path = self._messages_path_for_session(session_id)
        with self._lock:
            rows = self._read_session_messages_for_mutation(session_id)
            original_count = len(rows)
            cut_index: Optional[int] = None
            if frontend_message_id:
                for idx, row in enumerate(rows):
                    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
                    if meta.get("frontend_message_id") == frontend_message_id:
                        cut_index = idx
                        break
            if cut_index is None and keep_before_index is not None:
                try:
                    cut_index = max(0, min(int(keep_before_index), original_count))
                except Exception:
                    cut_index = None
            if cut_index is None:
                return {"removed_count": 0, "remaining_count": original_count}

            kept = rows[:cut_index]
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as f:
                for row in kept:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._refresh_session_summary_after_truncate(session_id, kept)
            return {"removed_count": original_count - len(kept), "remaining_count": len(kept)}

    def list_messages(self, session_id: str, limit: int = 40) -> List[Dict[str, Any]]:
        path = self._messages_path_for_session(session_id)
        if path.exists():
            return self._read_jsonl_messages(path)[-max(1, limit):]
        if not self.legacy_messages_path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        with self.legacy_messages_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("session_id") == session_id:
                    rows.append(row)
        return rows[-max(1, limit):]

    def session_messages_path(self, session_id: str) -> str:
        return str(self._messages_path_for_session(session_id))

    def save_pending_action(self, session_id: str, action: Dict[str, Any]) -> Dict[str, Any]:
        pending = dict(action)
        pending.setdefault("pending_id", f"pending_{uuid.uuid4().hex[:12]}")
        pending.setdefault("status", "waiting_user_accept")
        pending["session_id"] = session_id
        pending["updated_at"] = int(time.time())
        with self._lock:
            all_pending = self._read_json_obj(self.pending_path)
            items = [p for p in all_pending.get(session_id, []) if p.get("status") == "waiting_user_accept"]
            items.append(pending)
            all_pending[session_id] = items[-20:]
            self._write_json_obj(self.pending_path, all_pending)
        return pending

    def list_pending_actions(self, session_id: str) -> List[Dict[str, Any]]:
        all_pending = self._read_json_obj(self.pending_path)
        return list(all_pending.get(session_id) or [])

    def _read_json_obj(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_json_obj(self, path: Path, data: Dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _messages_path_for_session(self, session_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "default")).strip("._")
        if not safe_id:
            safe_id = "default"
        if len(safe_id) > 120:
            safe_id = f"{safe_id[:96]}_{abs(hash(session_id))}"
        return self.messages_dir / f"{safe_id}.jsonl"

    def _read_jsonl_messages(self, path: Path) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        return rows

    def _read_session_messages_for_mutation(self, session_id: str) -> List[Dict[str, Any]]:
        path = self._messages_path_for_session(session_id)
        if path.exists():
            return self._read_jsonl_messages(path)
        if not self.legacy_messages_path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        with self.legacy_messages_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict) and row.get("session_id") == session_id:
                    rows.append(row)
        return rows

    def _refresh_session_summary_after_truncate(self, session_id: str, kept_messages: List[Dict[str, Any]]) -> None:
        sessions = self._read_json_obj(self.sessions_path)
        current = dict(sessions.get(session_id) or {})
        current["updated_at"] = int(time.time())
        current["last_user_intent"] = ""
        current["last_assistant_message"] = ""
        current["conversation_summary"] = ""
        for row in reversed(kept_messages):
            if row.get("role") == "assistant":
                current["last_assistant_message"] = str(row.get("content") or "")
                break
        for row in reversed(kept_messages):
            if row.get("role") == "user":
                meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
                current["last_user_intent"] = str(meta.get("intent") or "")
                break
        if current.get("last_user_intent") or current.get("last_assistant_message"):
            brief = (
                f"最近意图：{current.get('last_user_intent') or 'unknown'}；"
                f"最近回复：{current.get('last_assistant_message') or ''}"
            )
            current["conversation_summary"] = {
                "active_goal": "",
                "current_tree": current.get("tree_summary") if isinstance(current.get("tree_summary"), dict) else {},
                "last_user_intent": current.get("last_user_intent") or "",
                "last_assistant_action": current.get("last_assistant_message") or "",
                "last_meaningful_edit": {},
                "pending_action": "",
                "user_preferences": [],
                "unresolved_questions": [],
                "source_scope": current.get("selected_file_version_ids") if isinstance(current.get("selected_file_version_ids"), list) else [],
                "cautions": ["summary rebuilt after rollback truncate without LLM"],
                "brief": brief,
            }
            current["conversation_summary_brief"] = brief
        sessions[session_id] = current
        self._write_json_obj(self.sessions_path, sessions)


session_store = JsonSessionStore()
