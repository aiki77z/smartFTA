"""Batch-clean entity_v2 labels using an OpenAI-compatible chat endpoint.

Reads cleaning inputs produced by generate_cleaning_inputs.py and sends each
row's ``prompt`` field to a chat-completions endpoint. The response is stored
as the cleaned assistant answer.

Output rows:
  {"id": 0, "cleaned_assistant": "[ENTITY]\\n...", "error": ""}

Usage:
  python 2stages/entity_v2_tools/run_entity_v2_cleaning.py
    --input 2stages\\data\\entity_v2_clean\\cleaning_inputs_style.jsonl
    --out 2stages\\data\\entity_v2_clean\\cleaned_answers.jsonl
    --endpoint https://api.openai.com/v1
    --api-key sk-...
    --model gpt-4o
    --workers 1
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from common import read_jsonl, write_jsonl


SYSTEM_PROMPT = (
    "You are an expert Chinese industrial-fault knowledge annotation assistant. "
    "Follow the instructions in the user message exactly. Output only the cleaned "
    "assistant answer, with no commentary. Do not treat equipment/component IDs "
    "such as MBX03CP005 or MBA53AA005 as measurement values."
)


def post_chat(endpoint: str, api_key: str, model: str, messages: list[dict[str, str]], timeout: int) -> str:
    base = endpoint.rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 4096,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def completed_ids(path: Path) -> set[int]:
    completed: set[int] = set()
    for row in read_jsonl(path):
        row_id = row.get("id")
        answer = str(row.get("cleaned_assistant", "")).strip()
        if isinstance(row_id, int) and answer and not row.get("error"):
            completed.add(row_id)
    return completed


def run_one(task: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    last_error = ""
    text = ""
    for attempt in range(1, args.retries + 2):
        try:
            text = post_chat(
                args.endpoint,
                args.api_key,
                args.model,
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": task["prompt"]},
                ],
                args.timeout,
            )
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError) as exc:
            last_error = str(exc)
            if attempt <= args.retries:
                time.sleep(args.retry_sleep)
    return {
        "id": task["id"],
        "cleaned_assistant": text.strip(),
        "error": last_error if not text else "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--endpoint", required=True, help="OpenAI-compatible base URL, e.g. https://api.openai.com/v1")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=5.0)
    parser.add_argument("--log-every", type=int, default=10)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    inputs = read_jsonl(args.input)
    selected = inputs[args.start_index :]
    if args.limit is not None:
        selected = selected[: args.limit]
    skip = completed_ids(args.out) if args.resume else set()
    tasks = [{"id": row["id"], "prompt": row["prompt"]} for row in selected if row["id"] not in skip]

    outputs: list[dict[str, Any]] = []
    if args.workers == 1:
        for idx, task in enumerate(tasks, start=1):
            outputs.append(run_one(task, args))
            if idx % args.log_every == 0 or idx == len(tasks):
                print(f"processed {idx}/{len(tasks)}")
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(run_one, task, args) for task in tasks]
            done = 0
            for future in as_completed(futures):
                outputs.append(future.result())
                done += 1
                if done % args.log_every == 0 or done == len(tasks):
                    print(f"processed {done}/{len(tasks)}")

    # Merge with existing completed rows when resuming.
    existing = read_jsonl(args.out) if args.out.exists() else []
    existing_by_id = {row["id"]: row for row in existing if isinstance(row.get("id"), int)}
    for row in outputs:
        existing_by_id[row["id"]] = row
    merged = [existing_by_id[row_id] for row_id in sorted(existing_by_id)]
    write_jsonl(args.out, merged)
    errors = [row for row in merged if row.get("error")]
    print(f"done={len(merged)} rows -> {args.out}; errors={len(errors)}")
    if errors:
        print("WARNING: some rows have errors; rerun with --resume to retry them.")


if __name__ == "__main__":
    main()
