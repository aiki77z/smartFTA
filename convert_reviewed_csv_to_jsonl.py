import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "reviewed_annotations.csv"
DEFAULT_OUTPUT = BASE_DIR / "final_annotations.jsonl"


def parse_json_cell(value: Any) -> List[Dict[str, Any]]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    text = str(value).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON cell: {text[:120]}") from exc
    if not isinstance(data, list):
        raise ValueError("JSON cell must be a list")
    return data


def blank_to_none(value: Any) -> Any:
    text = str(value or "").strip()
    return text if text else None


def build_sample(row: pd.Series) -> Dict[str, Any]:
    sample = {
        "sample_id": str(row.get("sample_id") or ""),
        "file_id": str(row.get("file_id") or row.get("source_doc_id") or ""),
        "chapter_id": str(row.get("chapter_id") or ""),
        "context_chunk_id": blank_to_none(row.get("context_chunk_id")),
        "target_chunk_id": str(row.get("target_chunk_id") or ""),
        "context_text": str(row.get("context_text") or ""),
        "text": str(row.get("text") or ""),
        "context_entities": parse_json_cell(row.get("context_entities_json", "[]")),
        "target_entities": parse_json_cell(row.get("target_entities_json", row.get("entities_json", "[]"))),
        "relations": parse_json_cell(row.get("relations_json", "[]")),
        "logic_groups": parse_json_cell(row.get("logic_groups_json", "[]")),
    }
    return sample


def validate_sample(sample: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    entity_ids = {
        entity.get("id")
        for entity in sample.get("context_entities", []) + sample.get("target_entities", [])
        if entity.get("id")
    }
    if not sample["sample_id"]:
        errors.append("sample_id is empty")
    if not sample["target_chunk_id"]:
        errors.append("target_chunk_id is empty")
    for relation in sample.get("relations", []):
        source = relation.get("source")
        target = relation.get("target")
        if source and source not in entity_ids:
            errors.append(f"relation {relation.get('id')} source not found: {source}")
        if target and target not in entity_ids:
            errors.append(f"relation {relation.get('id')} target not found: {target}")
    for group in sample.get("logic_groups", []):
        result = group.get("result")
        if result and result not in entity_ids:
            errors.append(f"logic group {group.get('id')} result not found: {result}")
        for member in group.get("members", []):
            if member and member not in entity_ids:
                errors.append(f"logic group {group.get('id')} member not found: {member}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert reviewed annotation CSV to final JSONL.")
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-jsonl", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--only-reviewed", action="store_true", help="只导出 status=reviewed 的样本。")
    parser.add_argument("--allow-errors", action="store_true", help="有校验错误时仍然输出。")
    args = parser.parse_args()

    df = pd.read_csv(args.input_csv, dtype=str, keep_default_na=False)
    if args.only_reviewed and "status" in df.columns:
        df = df[df["status"] == "reviewed"]

    samples = [build_sample(row) for _, row in df.iterrows()]
    all_errors: Dict[str, List[str]] = {}
    for sample in samples:
        errors = validate_sample(sample)
        if errors:
            all_errors[sample["sample_id"]] = errors

    if all_errors and not args.allow_errors:
        for sample_id, errors in all_errors.items():
            print(f"[ERROR] {sample_id}")
            for error in errors:
                print(f"  - {error}")
        raise SystemExit("校验失败。请修正 CSV，或使用 --allow-errors 强制导出。")

    output_path = Path(args.output_jsonl)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    print(f"已导出 {len(samples)} 条样本: {output_path}")
    if all_errors:
        print(f"存在 {len(all_errors)} 条样本有校验错误，已按 --allow-errors 要求继续导出。")


if __name__ == "__main__":
    main()
