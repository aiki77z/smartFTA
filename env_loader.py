from __future__ import annotations

import os
from pathlib import Path


def load_local_env(env_path: str | Path | None = None, *, override: bool = False) -> None:
    """Load KEY=VALUE pairs from the local .env file into os.environ."""
    path = Path(env_path) if env_path is not None else Path(__file__).with_name(".env")
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        if override or key not in os.environ:
            os.environ[key] = value

    # Backward compatibility: older local .env files used OPENAI_* names.
    # KB v2 uses provider-neutral LLM_* names.
    aliases = {
        "OPENAI_API_KEY": "LLM_API_KEY",
        "OPENAI_BASE_URL": "LLM_BASE_URL",
        "OPENAI_MODEL_NAME": "LLM_MODEL",
    }
    for old_key, new_key in aliases.items():
        old_value = os.environ.get(old_key)
        if old_value and (override or not os.environ.get(new_key)):
            os.environ[new_key] = old_value
