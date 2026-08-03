from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from env_loader import load_local_env

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore


_embedding_client: Optional[Any] = None
_embedding_cache: dict[str, list[float]] = {}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_catalog_name(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def build_top_event_semantic_text(
    name: str,
    *,
    normalized_name: str = "",
    aliases: Optional[list[str]] = None,
) -> str:
    clean_name = str(name or "").strip()
    clean_normalized_name = normalize_catalog_name(normalized_name or clean_name)
    clean_aliases = []
    seen = set()
    for alias in aliases or []:
        text = str(alias or "").strip()
        if not text:
            continue
        normalized = normalize_catalog_name(text)
        if normalized and normalized not in seen:
            seen.add(normalized)
            clean_aliases.append(normalized)

    lines = [
        f"top_event:{clean_name}",
        f"normalized_name:{clean_normalized_name}",
    ]
    if clean_aliases:
        lines.append(f"aliases:{' | '.join(clean_aliases)}")
    return "\n".join(line for line in lines if line.strip())


def load_top_event_embedding_env(env_file: str | Path = ".env") -> None:
    """Load embedding env from FTA-KB .env and entity_clustering/.env.

    The KB v2 pipeline usually maps ENTITY_CLUSTER_* variables before spawning
    import scripts. Standalone import/repair commands may not, so keep this
    small fallback here as well.
    """

    load_local_env(env_file, override=True)
    clustering_env = Path(__file__).resolve().parent / "entity_clustering" / ".env"
    if clustering_env.exists():
        load_local_env(clustering_env, override=False)

    mapping = {
        "ENTITY_CLUSTER_EMBEDDING_API_KEY": "EMBEDDING_API_KEY",
        "ENTITY_CLUSTER_EMBEDDING_BASE_URL": "EMBEDDING_BASE_URL",
        "ENTITY_CLUSTER_EMBEDDING_MODEL": "EMBEDDING_MODEL",
        "ENTITY_CLUSTER_EMBEDDING_BATCH_SIZE": "EMBEDDING_BATCH_SIZE",
    }
    for source, target in mapping.items():
        if not os.getenv(target) and os.getenv(source):
            os.environ[target] = os.getenv(source, "")


def embedding_model_name() -> str:
    return os.getenv("EMBEDDING_MODEL", "").strip()


def _embedding_openai() -> Optional[Any]:
    global _embedding_client
    if OpenAI is None:
        return None
    api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("LLM_API_KEY")
    model = embedding_model_name()
    if not api_key or not model:
        return None
    if _embedding_client is None:
        base_url = os.getenv("EMBEDDING_BASE_URL") or os.getenv("LLM_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        _embedding_client = OpenAI(api_key=api_key, base_url=base_url)
    return _embedding_client


def embed_strings_ordered(strings: list[str]) -> list[Optional[list[float]]]:
    if not strings or not embedding_model_name():
        return [None] * len(strings)
    client = _embedding_openai()
    if not client:
        return [None] * len(strings)

    unique: list[str] = []
    seen: dict[str, int] = {}
    for text in strings:
        key = text or ""
        if key not in seen:
            seen[key] = len(unique)
            unique.append(key)

    vectors_for_unique: list[Optional[list[float]]] = [None] * len(unique)
    to_request: list[str] = []
    request_slots: list[int] = []
    for index, text in enumerate(unique):
        if text in _embedding_cache:
            vectors_for_unique[index] = _embedding_cache[text]
        else:
            to_request.append(text)
            request_slots.append(index)

    batch_size = max(1, int(os.getenv("EMBEDDING_BATCH_SIZE", "10") or "10"))
    for start in range(0, len(to_request), batch_size):
        batch = to_request[start : start + batch_size]
        try:
            response = client.embeddings.create(model=embedding_model_name(), input=batch)
            for offset, item in enumerate(response.data):
                text = batch[offset]
                vector = list(item.embedding)
                _embedding_cache[text] = vector
                vectors_for_unique[request_slots[start + offset]] = vector
        except Exception:
            for offset, text in enumerate(batch):
                vectors_for_unique[request_slots[start + offset]] = _embedding_cache.get(text)

    lookup = {text: vectors_for_unique[index] for index, text in enumerate(unique)}
    return [lookup.get(text or "") for text in strings]


def build_top_event_embedding_fields(
    semantic_text: str,
    *,
    existing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "semantic_text": semantic_text,
        "embedding": None,
        "embedding_model": None,
        "embedding_updated_at": None,
    }
    model = embedding_model_name()
    if not semantic_text or not model:
        if existing and isinstance(existing.get("embedding"), list) and existing.get("embedding"):
            payload["embedding"] = existing.get("embedding")
            payload["embedding_model"] = existing.get("embedding_model")
            payload["embedding_updated_at"] = existing.get("embedding_updated_at")
        return payload

    if (
        existing
        and existing.get("semantic_text") == semantic_text
        and existing.get("embedding_model") == model
        and isinstance(existing.get("embedding"), list)
        and existing.get("embedding")
    ):
        payload["embedding"] = existing.get("embedding")
        payload["embedding_model"] = existing.get("embedding_model")
        payload["embedding_updated_at"] = existing.get("embedding_updated_at")
        return payload

    embedding = embed_strings_ordered([semantic_text])[0]
    if embedding:
        payload["embedding"] = embedding
        payload["embedding_model"] = model
        payload["embedding_updated_at"] = now_utc()
    return payload

