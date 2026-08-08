"""Run two-stage extraction: entity first, relation second, then stitch outputs.

Use ``--oracle-entity-candidates`` with relation/stitch to feed gold entities to
the relation model. This measures the relation-stage ceiling when entity
candidates are perfect.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SECTION_NAMES = ("ENTITY", "RELATION", "LOGIC_GROUP")


ENTITY_STAGE_NOTE = """

Two-stage extraction mode: ENTITY stage.
Only extract the [ENTITY] section. Keep [RELATION] and [LOGIC_GROUP] headers empty.
""".strip()


RELATION_STAGE_NOTE = """

Two-stage extraction mode: RELATION stage.
Use the provided [ENTITY_CANDIDATES] block as the closed entity set.
The source, target, members, and result fields must exactly copy a candidate normalized_name.
Do not create relations involving entities outside [ENTITY_CANDIDATES].
Keep [ENTITY] empty and output only [RELATION] and [LOGIC_GROUP] content.
""".strip()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no} in {path}: {exc}") from exc
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_optional_text(path: Path | None) -> str:
    if path is None:
        return ""
    return path.read_text(encoding="utf-8").strip()


def split_messages(row: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
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


def split_sections(text: str) -> dict[str, list[str]]:
    sections = {name: [] for name in SECTION_NAMES}
    current: str | None = None
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper in {f"[{name}]" for name in SECTION_NAMES}:
            current = upper.strip("[]")
            continue
        if current is not None and not is_header_line(current, line):
            sections[current].append(line)
    return sections


def is_header_line(section: str, line: str) -> bool:
    normalized = "".join(part.strip().lower() for part in line.split("|"))
    headers = {
        "ENTITY": "mentiontypenormalized_nameevidence",
        "RELATION": "sourcerelation_typetargetcross_chunkinvolved_chunk_idsevidencepolaritycertainty",
        "LOGIC_GROUP": "logic_typemembersresultinvolved_chunk_idsevidence",
    }
    return normalized == headers[section]


def render_answer(
    entity_lines: list[str] | None = None,
    relation_lines: list[str] | None = None,
    logic_lines: list[str] | None = None,
) -> str:
    entity = "\n".join(entity_lines or [])
    relation = "\n".join(relation_lines or [])
    logic = "\n".join(logic_lines or [])
    return f"[ENTITY]\n{entity}\n\n[RELATION]\n{relation}\n\n[LOGIC_GROUP]\n{logic}".strip()


def add_system_note(messages: list[dict[str, str]], note: str) -> list[dict[str, str]]:
    updated: list[dict[str, str]] = []
    added = False
    for message in messages:
        if message["role"] == "system" and not added:
            updated.append({"role": "system", "content": message["content"].rstrip() + "\n\n" + note})
            added = True
        else:
            updated.append(dict(message))
    if not added:
        updated.insert(0, {"role": "system", "content": note})
    return updated


def add_user_suffix(messages: list[dict[str, str]], suffix: str) -> list[dict[str, str]]:
    updated = [dict(message) for message in messages]
    for idx in range(len(updated) - 1, -1, -1):
        if updated[idx]["role"] == "user":
            updated[idx]["content"] = updated[idx]["content"].rstrip() + "\n\n" + suffix
            return updated
    updated.append({"role": "user", "content": suffix})
    return updated


def candidate_maps(entity_text: str) -> tuple[str, dict[str, str], set[str]]:
    rows = ["[ENTITY_CANDIDATES]", "normalized_name | type | mention"]
    alias_to_normalized: dict[str, str] = {}
    normalized_names: set[str] = set()
    for line in split_sections(entity_text)["ENTITY"]:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 3:
            continue
        mention, entity_type, normalized_name = fields[:3]
        normalized_name = normalized_name or mention
        rows.append(f"{normalized_name} | {entity_type} | {mention}")
        normalized_names.add(normalized_name)
        alias_to_normalized[normalized_name] = normalized_name
        alias_to_normalized[mention] = normalized_name
    return "\n".join(rows), alias_to_normalized, normalized_names


def normalize_relation_lines(
    relation_text: str,
    alias_to_normalized: dict[str, str],
    normalized_names: set[str],
    drop_invalid: bool,
) -> tuple[list[str], list[str]]:
    sections = split_sections(relation_text)
    relation_lines: list[str] = []
    for line in sections["RELATION"]:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 3:
            continue
        fields[0] = alias_to_normalized.get(fields[0], fields[0])
        fields[2] = alias_to_normalized.get(fields[2], fields[2])
        if drop_invalid and (fields[0] not in normalized_names or fields[2] not in normalized_names):
            continue
        relation_lines.append(" | ".join(fields))

    logic_lines: list[str] = []
    for line in sections["LOGIC_GROUP"]:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 3:
            continue
        members = [alias_to_normalized.get(item.strip(), item.strip()) for item in fields[1].split(";") if item.strip()]
        result = alias_to_normalized.get(fields[2], fields[2])
        if drop_invalid and (result not in normalized_names or any(member not in normalized_names for member in members)):
            continue
        fields[1] = ";".join(members)
        fields[2] = result
        logic_lines.append(" | ".join(fields))

    return relation_lines, logic_lines


def post_json(endpoint: str, token: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def completed_ids(path: Path) -> set[int]:
    completed: set[int] = set()
    for row in read_jsonl(path):
        row_id = row.get("id")
        prediction = str(row.get("prediction", "")).strip()
        if isinstance(row_id, int) and prediction and not row.get("error"):
            completed.add(row_id)
    return completed


def run_task(task: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    last_error = ""
    response: dict[str, Any] | None = None
    for attempt in range(1, args.retries + 2):
        try:
            response = post_json(args.endpoint, args.token, task["payload"], args.timeout)
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


def run_tasks(tasks: list[dict[str, Any]], output: Path, args: argparse.Namespace) -> None:
    if not tasks:
        print("nothing to do")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"queued {len(tasks)} rows, workers={args.workers}")
    if args.workers == 1:
        for idx, task in enumerate(tasks, start=1):
            append_jsonl(output, run_task(task, args))
            if idx % args.log_every == 0 or idx == len(tasks):
                print(f"processed {idx}/{len(tasks)}")
        return

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(run_task, task, args) for task in tasks]
        done = 0
        for future in as_completed(futures):
            append_jsonl(output, future.result())
            done += 1
            if done % args.log_every == 0 or done == len(tasks):
                print(f"processed {done}/{len(tasks)}")


def select_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[tuple[int, dict[str, Any]]]:
    selected = list(enumerate(rows[args.start_index :], start=args.start_index))
    if args.limit is not None:
        selected = selected[: args.limit]
    return selected


def stage_entity(args: argparse.Namespace) -> None:
    rows = read_jsonl(args.input)
    skip = completed_ids(args.entity_output) if args.resume else set()
    entity_rules = read_optional_text(args.entity_rules_file)
    entity_note = ENTITY_STAGE_NOTE if not entity_rules else ENTITY_STAGE_NOTE + "\n\n" + entity_rules
    tasks: list[dict[str, Any]] = []
    for idx, row in select_rows(rows, args):
        if idx in skip:
            continue
        prompt_messages, gold = split_messages(row)
        messages = add_system_note(prompt_messages, entity_note)
        tasks.append(
            {
                "id": idx,
                "gold": gold,
                "messages": messages,
                "payload": {"messages": messages, "temperature": args.temperature, "max_tokens": args.max_tokens},
            }
        )
    run_tasks(tasks, args.entity_output, args)


def stage_relation(args: argparse.Namespace) -> None:
    rows = read_jsonl(args.input)
    entity_by_id = {row["id"]: row for row in read_jsonl(args.entity_output) if isinstance(row.get("id"), int)}
    skip = completed_ids(args.relation_output) if args.resume else set()
    tasks: list[dict[str, Any]] = []
    for idx, row in select_rows(rows, args):
        if idx in skip:
            continue
        prompt_messages, gold = split_messages(row)
        if args.oracle_entity_candidates:
            entity_text = gold
        else:
            entity_row = entity_by_id.get(idx)
            if entity_row is None:
                raise ValueError(f"Missing entity prediction for row id {idx}")
            entity_text = entity_row.get("prediction", "")
        candidate_block, _alias_to_normalized, _normalized_names = candidate_maps(entity_text)
        messages = add_system_note(prompt_messages, RELATION_STAGE_NOTE)
        messages = add_user_suffix(messages, candidate_block)
        tasks.append(
            {
                "id": idx,
                "gold": gold,
                "messages": messages,
                "payload": {"messages": messages, "temperature": args.temperature, "max_tokens": args.max_tokens},
            }
        )
    run_tasks(tasks, args.relation_output, args)


def stage_stitch(args: argparse.Namespace) -> None:
    rows = read_jsonl(args.input)
    entity_by_id = {row["id"]: row for row in read_jsonl(args.entity_output) if isinstance(row.get("id"), int)}
    relation_by_id = {row["id"]: row for row in read_jsonl(args.relation_output) if isinstance(row.get("id"), int)}
    stitched: list[dict[str, Any]] = []
    for idx, row in select_rows(rows, args):
        prompt_messages, gold = split_messages(row)
        relation_row = relation_by_id.get(idx)
        entity_row = None if args.oracle_entity_candidates else entity_by_id.get(idx)
        if (not args.oracle_entity_candidates and entity_row is None) or relation_row is None:
            raise ValueError(f"Missing stage output for row id {idx}")

        entity_text = gold if args.oracle_entity_candidates else entity_row.get("prediction", "")
        relation_text = relation_row.get("prediction", "")
        entity_lines = split_sections(entity_text)["ENTITY"]
        _candidate_block, alias_to_normalized, normalized_names = candidate_maps(entity_text)
        relation_lines, logic_lines = normalize_relation_lines(
            relation_text,
            alias_to_normalized,
            normalized_names,
            drop_invalid=not args.keep_invalid_relations,
        )
        errors = [relation_row.get("error", "")]
        if entity_row is not None:
            errors.append(entity_row.get("error", ""))
        errors = [value for value in errors if value]
        stitched.append(
            {
                "id": idx,
                "prediction": render_answer(entity_lines, relation_lines, logic_lines),
                "gold": gold,
                "messages": prompt_messages,
                "error": " | ".join(errors),
            }
        )
    write_jsonl(args.final_output, stitched)
    print(f"stitched={len(stitched)} -> {args.final_output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["entity", "relation", "stitch"], required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--entity-output", type=Path, required=True)
    parser.add_argument("--relation-output", type=Path, required=True)
    parser.add_argument("--final-output", type=Path, required=True)
    parser.add_argument("--endpoint", default="", help="Required for entity/relation stages")
    parser.add_argument("--token", default="")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=5.0)
    parser.add_argument("--log-every", type=int, default=10)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--keep-invalid-relations", action="store_true")
    parser.add_argument(
        "--entity-rules-file",
        type=Path,
        help="Optional prompt addendum for entity-stage normalization rules.",
    )
    parser.add_argument(
        "--oracle-entity-candidates",
        action="store_true",
        help="Use gold [ENTITY] lines as relation candidates for oracle relation experiments.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("--workers must be >= 1")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be >= 1")
    if args.stage in {"entity", "relation"} and not args.endpoint:
        raise ValueError("--endpoint is required for entity/relation stages")

    if args.stage == "entity":
        stage_entity(args)
    elif args.stage == "relation":
        stage_relation(args)
    else:
        stage_stitch(args)


if __name__ == "__main__":
    main()
