"""HTTP wrapper for running the LLM extraction baseline on a remote server."""

from __future__ import annotations

import os
from typing import Any

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


DEFAULT_SYSTEM_GUARD = (
    "请严格按用户给定格式输出，只输出 [ENTITY]、[RELATION]、[LOGIC_GROUP] 三个段落，"
    "不要输出解释、Markdown 代码块或 JSON。"
)


class ChatMessage(BaseModel):
    role: str
    content: str


class ExtractRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    temperature: float = 0.0
    max_tokens: int = 2048
    top_p: float = 1.0
    extra_body: dict[str, Any] = Field(default_factory=dict)


class ExtractResponse(BaseModel):
    text: str
    model: str
    usage: dict[str, Any] = Field(default_factory=dict)


app = FastAPI(title="smartFTA remote LLM baseline", version="0.1.0")


def _settings() -> dict[str, str]:
    return {
        "model": os.environ.get("LLM_MODEL", "local-model"),
        "api_base": os.environ.get("LLM_API_BASE", "http://127.0.0.1:8000/v1").rstrip("/"),
        "api_key": os.environ.get("LLM_API_KEY", "EMPTY"),
        "baseline_token": os.environ.get("BASELINE_TOKEN", ""),
    }


def _check_token(authorization: str | None, expected_token: str) -> None:
    if not expected_token:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected_token:
        raise HTTPException(status_code=403, detail="Invalid bearer token")


def _normalize_messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
    normalized = [{"role": m.role, "content": m.content} for m in messages]
    if normalized and normalized[0]["role"] == "system":
        normalized[0]["content"] = normalized[0]["content"].rstrip() + "\n\n" + DEFAULT_SYSTEM_GUARD
    else:
        normalized.insert(0, {"role": "system", "content": DEFAULT_SYSTEM_GUARD})
    return normalized


@app.get("/health")
def health() -> dict[str, str]:
    settings = _settings()
    return {"status": "ok", "model": settings["model"], "api_base": settings["api_base"]}


@app.post("/extract", response_model=ExtractResponse)
def extract(payload: ExtractRequest, authorization: str | None = Header(default=None)) -> ExtractResponse:
    settings = _settings()
    _check_token(authorization, settings["baseline_token"])

    body: dict[str, Any] = {
        "model": settings["model"],
        "messages": _normalize_messages(payload.messages),
        "temperature": payload.temperature,
        "top_p": payload.top_p,
        "max_tokens": payload.max_tokens,
    }
    body.update(payload.extra_body)

    try:
        response = requests.post(
            f"{settings['api_base']}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings['api_key']}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=600,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"LLM backend request failed: {exc}") from exc

    data = response.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=f"Unexpected LLM response: {data}") from exc

    return ExtractResponse(text=text.strip(), model=settings["model"], usage=data.get("usage", {}))

