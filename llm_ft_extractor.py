from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm_annotation_extractor import (
    append_jsonl,
    build_error_result,
    build_prompt,
    build_row,
    call_llm,
    explain_context_decision,
    get_chunk_id,
    get_chunk_text,
    parse_logic_groups,
    parse_relations,
    parse_unified_entities,
    sort_chunks,
    split_sections,
    write_csv,
)


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


def build_entity_candidates(entity_text: str) -> str:
    rows = ["[ENTITY_CANDIDATES]", "normalized_name | type | mention"]
    for line in split_sections(entity_text)["ENTITY"]:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 3:
            continue
        mention, entity_type, normalized_name = fields[:3]
        normalized_name = normalized_name or mention
        rows.append(f"{normalized_name} | {entity_type} | {mention}")
    return "\n".join(rows)


def process_chunk_two_stage(
    *,
    index: int,
    total: int,
    chunk: Dict[str, Any],
    prev_chunk: Optional[Dict[str, Any]],
    client: Any,
    entity_model: str,
    relation_model: str,
    entity_rules: str,
    temperature: float,
    max_tokens: int,
    dry_run: bool,
    debug_context: bool,
    sleep_seconds: float,
) -> Dict[str, Any]:
    use_context, context_reason = explain_context_decision(prev_chunk, chunk)
    context_chunk = prev_chunk if use_context else None
    context_chunk_id = get_chunk_id(context_chunk) if context_chunk else ""
    target_chunk_id = get_chunk_id(chunk)
    context_text = get_chunk_text(context_chunk)
    target_text = get_chunk_text(chunk)
    base_prompt = build_prompt(context_text, target_text, context_chunk_id, target_chunk_id)

    entity_prompt = base_prompt + "\n\n" + ENTITY_STAGE_NOTE
    if entity_rules:
        entity_prompt = entity_prompt + "\n\n" + entity_rules
    relation_prompt = ""

    if dry_run:
        entity_text = ""
        relation_text = ""
        entities: List[Dict[str, Any]] = []
        context_entities: List[Dict[str, Any]] = []
        target_entities: List[Dict[str, Any]] = []
        relations: List[Dict[str, Any]] = []
        logic_groups: List[Dict[str, Any]] = []
    else:
        entity_text = call_llm(client, entity_model, entity_prompt, temperature, max_tokens)
        candidate_block = build_entity_candidates(entity_text)
        relation_prompt = base_prompt + "\n\n" + RELATION_STAGE_NOTE + "\n\n" + candidate_block
        relation_text = call_llm(client, relation_model, relation_prompt, temperature, max_tokens)

        entities, name_to_id = parse_unified_entities(split_sections(entity_text)["ENTITY"])
        relations = parse_relations(
            split_sections(relation_text)["RELATION"],
            name_to_id,
            target_chunk_id,
        )
        logic_groups = parse_logic_groups(
            split_sections(relation_text)["LOGIC_GROUP"],
            name_to_id,
            target_chunk_id,
        )
        context_entities = []
        target_entities = []

    if sleep_seconds > 0:
        time.sleep(sleep_seconds)

    row = build_row(chunk, context_chunk, entities, context_entities, target_entities, relations, logic_groups)
    model = f"{entity_model}|{relation_model}" if not dry_run else "dry-run"
    raw_record = {
        "sample_id": row["sample_id"],
        "file_id": row["file_id"],
        "chapter_id": row["chapter_id"],
        "source_type": row["source_type"],
        "context_chunk_id": context_chunk_id or None,
        "target_chunk_id": target_chunk_id,
        "uses_context": bool(context_chunk),
        "context_reason": context_reason,
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prompt": base_prompt,
        "entity_prompt": entity_prompt,
        "relation_prompt": relation_prompt,
        "entity_llm_output": entity_text,
        "llm_output": relation_text,
    }
    debug_suffix = f" reason={context_reason}" if debug_context else ""
    log_line = (
        f"[{index + 1}/{total}] {row['sample_id']} "
        f"context={bool(context_chunk)}{debug_suffix} "
        f"entities={len(entities)} relations={len(relations)} logic_groups={len(logic_groups)}"
    )
    return {"index": index, "row": row, "raw_record": raw_record, "log_line": log_line}


def generate_annotation_dataset_two_stage(
    *,
    chunks: List[Dict[str, Any]],
    output_csv: Path,
    raw_jsonl: Path,
    base_url: str,
    api_key: str,
    entity_model: str,
    relation_model: str,
    entity_rules_file: str = "",
    temperature: float = 0.0,
    max_tokens: int = 2200,
    workers: int = 1,
    dry_run: bool = False,
    debug_context: bool = False,
    sleep_seconds: float = 0.0,
    continue_on_error: bool = True,
) -> Dict[str, Any]:
    if output_csv.exists():
        output_csv.unlink()
    if raw_jsonl.exists():
        raw_jsonl.unlink()

    rules_path = Path(entity_rules_file) if entity_rules_file else None
    if rules_path is not None and not rules_path.is_absolute():
        rules_path = Path(__file__).resolve().parent / rules_path
    entity_rules = rules_path.read_text(encoding="utf-8").strip() if rules_path and rules_path.exists() else ""

    chunks = sort_chunks(chunks)
    client = None
    if not dry_run:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)

    indexed_inputs = [(index, chunk, chunks[index - 1] if index > 0 else None) for index, chunk in enumerate(chunks)]
    results: List[Dict[str, Any]] = []
    common = {
        "client": client,
        "entity_model": entity_model,
        "relation_model": relation_model,
        "entity_rules": entity_rules,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "dry_run": dry_run,
        "debug_context": debug_context,
        "sleep_seconds": sleep_seconds,
    }

    if workers <= 1:
        for index, chunk, prev_chunk in indexed_inputs:
            try:
                result = process_chunk_two_stage(
                    index=index,
                    total=len(chunks),
                    chunk=chunk,
                    prev_chunk=prev_chunk,
                    **common,
                )
            except Exception as exc:
                if not continue_on_error:
                    raise
                result = build_error_result(index, len(chunks), chunk, prev_chunk, f"{entity_model}|{relation_model}", exc)
            print(result["log_line"])
            results.append(result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(
                    process_chunk_two_stage,
                    index=index,
                    total=len(chunks),
                    chunk=chunk,
                    prev_chunk=prev_chunk,
                    **common,
                ): (index, chunk, prev_chunk)
                for index, chunk, prev_chunk in indexed_inputs
            }
            for future in as_completed(future_map):
                index, chunk, prev_chunk = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    if not continue_on_error:
                        raise
                    result = build_error_result(index, len(chunks), chunk, prev_chunk, f"{entity_model}|{relation_model}", exc)
                print(result["log_line"])
                results.append(result)

    results.sort(key=lambda item: item["index"])
    rows = [item["row"] for item in results]
    write_csv(output_csv, rows)
    for item in results:
        append_jsonl(raw_jsonl, item["raw_record"])
    return {
        "samples": len(rows),
        "failed": sum(1 for item in results if item.get("failed")),
        "output_csv": str(output_csv),
        "raw_jsonl": str(raw_jsonl),
    }
