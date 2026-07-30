"""Call a remote LLM extraction service over a local dataset JSONL file."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no}: {exc}") from exc
    return rows


def _split_messages(row: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
    messages = row.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Each row must contain a messages list")

    prompt_messages: list[dict[str, str]] = []
    gold = ""
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role in {"system", "user"}:
            prompt_messages.append({"role": role, "content": content})
        elif role == "assistant" and not gold:
            gold = content
    return prompt_messages, gold


def _post_json(endpoint: str, token: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _read_completed_ids(path: Path) -> set[int]:
    completed: set[int] = set()
    if not path.exists():
        return completed
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row_id = row.get("id")
            if isinstance(row_id, int):
                completed.add(row_id)
    return completed


def _build_task(idx: int, row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    prompt_messages, gold = _split_messages(row)
    return {
        "id": idx,
        "gold": gold,
        "messages": prompt_messages,
        "payload": {
            "messages": prompt_messages,
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
        },
    }


def _run_task(task: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    last_error = ""
    response: dict[str, Any] | None = None
    for attempt in range(1, args.retries + 2):
        try:
            response = _post_json(args.endpoint, args.token, task["payload"], args.timeout)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            if attempt <= args.retries:
                time.sleep(args.retry_sleep)

    return {
        "id": task["id"],
        "prediction": "" if response is None else response.get("text", ""),
        "gold": task["gold"],
        "messages": task["messages"],
        "error": last_error if response is None else "",
    }


def run(args: argparse.Namespace) -> None:
    if args.workers < 1:
        raise ValueError("--workers must be >= 1")
    if args.start_index < 0:
        raise ValueError("--start-index must be >= 0")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be >= 1")

    rows = _read_jsonl(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    completed_ids = _read_completed_ids(args.output) if args.resume else set()
    selected_rows = list(enumerate(rows[args.start_index :], start=args.start_index))
    if args.limit is not None:
        selected_rows = selected_rows[: args.limit]

    tasks = [
        _build_task(idx, row, args)
        for idx, row in selected_rows
        if idx not in completed_ids
    ]

    if not tasks:
        print(f"nothing to do: {len(completed_ids)} existing rows skipped")
        return

    mode = "a" if args.resume and args.output.exists() else "w"
    with args.output.open(mode, encoding="utf-8") as out:
        done = 0
        total = len(tasks)
        print(f"queued {total} rows, skipped {len(completed_ids)} existing rows, workers={args.workers}")

        if args.workers == 1:
            for task in tasks:
                result = _run_task(task, args)
                out.write(json.dumps(result, ensure_ascii=False) + "\n")
                out.flush()
                done += 1
                if done % args.log_every == 0 or done == total:
                    print(f"processed {done}/{total}")
            return

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(_run_task, task, args) for task in tasks]
            for future in as_completed(futures):
                result = future.result()
                out.write(json.dumps(result, ensure_ascii=False) + "\n")
                out.flush()
                done += 1
                if done % args.log_every == 0 or done == total:
                    print(f"processed {done}/{total}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Input SFT messages JSONL")
    parser.add_argument("--output", type=Path, required=True, help="Output prediction JSONL")
    parser.add_argument("--endpoint", required=True, help="Remote endpoint, e.g. http://server:9000/extract")
    parser.add_argument("--token", default="", help="Bearer token matching BASELINE_TOKEN on server")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=5.0)
    parser.add_argument("--log-every", type=int, default=10)
    parser.add_argument("--resume", action="store_true", help="Append and skip ids already present in output")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent request workers")
    parser.add_argument("--start-index", type=int, default=0, help="Start from this input row id")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to queue after start-index")
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
