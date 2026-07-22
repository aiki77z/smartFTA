import html
import json
from pathlib import Path
from typing import Any
from datetime import datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DRAFT = BASE_DIR / "ai_annotation_draft.csv"
DEFAULT_OUTPUT = BASE_DIR / "reviewed_annotations.csv"
UPLOADED_CACHE = BASE_DIR / "_uploaded_annotations.csv"

JSON_COLUMNS = [
    "entities_json",
    "context_entities_json",
    "target_entities_json",
    "relations_json",
    "logic_groups_json",
]
REQUIRED_COLUMNS = [
    "sample_id",
    "file_id",
    "chapter_id",
    "source_type",
    "context_chunk_id",
    "target_chunk_id",
    "context_text",
    "text",
    *JSON_COLUMNS,
    "status",
    "reviewer_notes",
]
STATUS_OPTIONS = ["draft", "reviewing", "reviewed", "needs_fix"]
ENTITY_TYPES = ["故障事件", "故障类别", "报警码", "维修方法", "触发规则"]
NESTED_LIST_COLUMNS = {"members", "involved_chunk_ids"}
TRANSIENT_COLUMNS = {"_selected", "_linked"}


def parse_json_cell(value: Any) -> list[dict[str, Any]]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, list):
        return value
    text = str(value).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def parse_editor_cell(key: str, value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return [] if key in NESTED_LIST_COLUMNS else ""
    if isinstance(value, (list, dict, bool, int)):
        return value
    text = str(value).strip()
    if not text:
        return [] if key in NESTED_LIST_COLUMNS else ""
    if text[0] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if key in NESTED_LIST_COLUMNS:
        return [item.strip() for item in text.replace("，", ";").replace(",", ";").split(";") if item.strip()]
    return text


def rows_without_transient(df: pd.DataFrame) -> list[dict[str, Any]]:
    records = []
    for record in df.to_dict("records"):
        cleaned = {k: parse_editor_cell(k, v) for k, v in record.items() if k not in TRANSIENT_COLUMNS}
        has_value = any(str(v).strip() for v in cleaned.values() if not isinstance(v, list))
        has_list = any(isinstance(v, list) and v for v in cleaned.values())
        if has_value or has_list:
            records.append(cleaned)
    return records


def dump_json_cell(value: Any) -> str:
    records = rows_without_transient(value) if isinstance(value, pd.DataFrame) else value
    return json.dumps(records or [], ensure_ascii=False)


def stringify_nested_cells(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        out[col] = out[col].apply(
            lambda value: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
        )
    for col in ["start", "end"]:
        if col in out.columns:
            out[col] = out[col].apply(format_position_cell)
    return out


def format_position_cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    try:
        return str(int(float(text)))
    except ValueError:
        return text


def load_data(path_or_file: Any) -> pd.DataFrame:
    df = pd.read_csv(path_or_file, dtype=str, keep_default_na=False)
    if "file_id" not in df.columns and "source_doc_id" in df.columns:
        df["file_id"] = df["source_doc_id"]
    if "target_entities_json" not in df.columns and "entities_json" in df.columns:
        df["target_entities_json"] = df["entities_json"]
    for col in REQUIRED_COLUMNS:
        df[col] = df[col] if col in df.columns else ("[]" if col in JSON_COLUMNS else "")
    if "entities_json" in df.columns:
        df["entities_json"] = df.apply(
            lambda row: row["entities_json"]
            if str(row.get("entities_json", "")).strip() not in {"", "[]"}
            else json.dumps(
                parse_json_cell(row.get("context_entities_json", "[]")) + parse_json_cell(row.get("target_entities_json", "[]")),
                ensure_ascii=False,
            ),
            axis=1,
        )
    for col in JSON_COLUMNS:
        df[col] = df[col].apply(lambda value: json.dumps(parse_json_cell(value), ensure_ascii=False))
    df["status"] = df["status"].replace("", "draft")
    return df[REQUIRED_COLUMNS]


def empty_dataset() -> pd.DataFrame:
    return pd.DataFrame(columns=REQUIRED_COLUMNS)


def list_available_csv_files() -> list[Path]:
    return sorted(
        [
            path
            for path in BASE_DIR.glob("*.csv")
            if path.is_file() and path.name != UPLOADED_CACHE.name
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def choose_initial_csv() -> Path | None:
    reviewed_versions = sorted(
        BASE_DIR.glob("reviewed_annotations_*.csv"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in [*reviewed_versions, DEFAULT_OUTPUT, DEFAULT_DRAFT, *list_available_csv_files()]:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def make_timestamped_review_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = BASE_DIR / f"reviewed_annotations_{timestamp}.csv"
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = BASE_DIR / f"reviewed_annotations_{timestamp}_{index}.csv"
        if not candidate.exists():
            return candidate
    return BASE_DIR / f"reviewed_annotations_{timestamp}_{datetime.now().microsecond}.csv"


def start_new_output_file() -> Path:
    path = make_timestamped_review_path()
    st.session_state.output_path = str(path)
    return path


def current_output_path() -> Path:
    if not st.session_state.get("output_path"):
        return start_new_output_file()
    return Path(str(st.session_state.output_path))


def save_data(df: pd.DataFrame, path: Path | None = None) -> None:
    path = path or current_output_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
        if "last_save_warning" in st.session_state:
            st.session_state.pop("last_save_warning", None)
    except PermissionError:
        fallback = path.with_name(f"{path.stem}_autosave_{datetime.now().strftime('%Y%m%d_%H%M%S')}{path.suffix}")
        df.to_csv(fallback, index=False, encoding="utf-8-sig")
        try:
            st.session_state.output_path = str(fallback)
        except Exception:
            pass
        try:
            st.session_state.last_save_warning = (
                f"{path.name} is locked, so changes were saved to {fallback.name}. "
                "Close the CSV in Excel/WPS and save again."
            )
        except Exception:
            pass


def ensure_session_state() -> None:
    if "df" not in st.session_state:
        source = choose_initial_csv()
        if source:
            st.session_state.df = load_data(source)
            st.session_state.loaded_path = str(source)
            st.session_state.loaded_display_name = source.name
        else:
            st.session_state.df = empty_dataset()
            st.session_state.loaded_path = ""
            st.session_state.loaded_display_name = ""
    if "output_path" not in st.session_state:
        start_new_output_file()
    if "selected_sample_id" not in st.session_state:
        st.session_state.selected_sample_id = st.session_state.df.iloc[0]["sample_id"] if len(st.session_state.df) else ""
    st.session_state.setdefault("undo_stack", {})
    st.session_state.setdefault("redo_stack", {})
    st.session_state.setdefault("last_saved_snapshot", {})
    st.session_state.setdefault("editor_versions", {})
    st.session_state.setdefault("editor_drafts", {})
    st.session_state.setdefault("linked_entity_ids", {})
    st.session_state.setdefault("scroll_to_top", False)
    st.session_state.setdefault("loaded_display_name", "")


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.15rem; max-width: 100%; }
        div[data-testid="stButton"] button { min-height: 2.85rem; }
        .status-title {
            font-size: 1.12rem;
            font-weight: 700;
            color: #0f4c81;
            padding: 0.55rem 0.75rem;
            margin: 0.1rem 0 0.4rem 0;
            border-left: 5px solid #1f77b4;
            background: #eef6ff;
            border-radius: 6px;
        }
        .status-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 155px;
            padding: 10px 16px;
            margin: 0.1rem 0 0.65rem 0;
            border-radius: 8px;
            color: white;
            font-size: 1.55rem;
            font-weight: 800;
            letter-spacing: 0;
        }
        .status-draft { background: #92400e; }
        .status-reviewing { background: #1d4ed8; }
        .status-reviewed { background: #15803d; }
        .status-needs_fix { background: #b91c1c; }
        .nav-button-note {
            color: #666;
            font-size: 0.85rem;
            margin-top: 0.55rem;
            margin-bottom: 0.5rem;
        }
        .linked-help {
            color: #6b7280;
            font-size: 0.85rem;
            margin: -0.15rem 0 0.45rem 0;
        }
        .table-bottom-gap { height: 22px; }
        .evidence-preview-title {
            margin-top: -10px;
            margin-bottom: 4px;
            font-weight: 700;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def scroll_to_top_if_needed() -> None:
    if not st.session_state.pop("scroll_to_top", False):
        return
    components.html(
        """
        <script>
        function jumpTop() {
          try {
            window.parent.scrollTo({top: 0, left: 0, behavior: "smooth"});
            const doc = window.parent.document;
            doc.documentElement.scrollTop = 0;
            doc.body.scrollTop = 0;
            ["[data-testid='stAppViewContainer']", "section.main", "main"].forEach((q) => {
              doc.querySelectorAll(q).forEach((el) => { el.scrollTop = 0; });
            });
          } catch (e) {}
        }
        jumpTop();
        setTimeout(jumpTop, 80);
        setTimeout(jumpTop, 260);
        </script>
        """,
        height=0,
    )


def load_dataset(path_or_file: Any, loaded_name: str | None = None) -> None:
    st.session_state.df = load_data(path_or_file)
    st.session_state.loaded_path = str(path_or_file) if isinstance(path_or_file, (str, Path)) else loaded_name or ""
    st.session_state.loaded_display_name = loaded_name or Path(str(path_or_file)).name
    st.session_state.selected_sample_id = st.session_state.df.iloc[0]["sample_id"] if len(st.session_state.df) else ""
    st.session_state.undo_stack = {}
    st.session_state.redo_stack = {}
    st.session_state.last_saved_snapshot = {}
    st.session_state.editor_versions = {}
    st.session_state.editor_drafts = {}
    st.session_state.linked_entity_ids = {}
    st.session_state.scroll_to_top = True
    start_new_output_file()
    save_data(st.session_state.df)


def reload_current_dataset() -> None:
    loaded_path = Path(str(st.session_state.get("loaded_path", "")))
    if loaded_path.exists():
        load_dataset(loaded_path)
    else:
        source = choose_initial_csv()
        if source:
            load_dataset(source)
        else:
            st.session_state.df = empty_dataset()
            st.session_state.loaded_path = ""
            st.session_state.loaded_display_name = ""
            st.session_state.selected_sample_id = ""
            start_new_output_file()


def sample_label(row: pd.Series) -> str:
    return f"{row['sample_id']}  [{row.get('status', 'draft') or 'draft'}]"


def sample_label_by_id(df: pd.DataFrame, sample_id: str) -> str:
    rows = df[df["sample_id"] == sample_id]
    return sample_label(rows.iloc[0]) if not rows.empty else str(sample_id)


def get_sample_position(sample_ids: list[str], sample_id: str) -> int:
    try:
        return sample_ids.index(sample_id)
    except ValueError:
        return 0


def sync_sidebar_choice() -> None:
    st.session_state.selected_sample_id = st.session_state.sidebar_choice_id
    st.session_state.scroll_to_top = True


def render_sidebar() -> pd.DataFrame | None:
    with st.sidebar:
        st.header("样本")
        uploaded = st.file_uploader("上传 CSV", type=["csv"])
        if uploaded is not None:
            try:
                upload_key = f"{uploaded.name}:{uploaded.size}"
                if st.session_state.get("last_uploaded_key") != upload_key:
                    UPLOADED_CACHE.write_bytes(uploaded.getvalue())
                    st.session_state.last_uploaded_key = upload_key
                    load_dataset(UPLOADED_CACHE, uploaded.name)
                    st.rerun()
                else:
                    st.success(f"已加载 {uploaded.name}，共 {len(st.session_state.df)} 条样本。")
            except Exception as exc:
                st.error(f"导入失败：{exc}")
                return None

        csv_files = list_available_csv_files()
        if csv_files:
            csv_names = [path.name for path in csv_files]
            selected_csv = st.selectbox("从当前目录选择 CSV", csv_names, key="folder_csv_choice")
            if st.button("加载选中的 CSV", use_container_width=True):
                try:
                    load_dataset(BASE_DIR / selected_csv)
                    st.success(f"已加载 {selected_csv}，共 {len(st.session_state.df)} 条样本。")
                    st.rerun()
                except Exception as exc:
                    st.error(f"加载失败：{exc}")
                    return None
        else:
            st.info("当前 annotation-data-pipeline 目录下没有 CSV 文件。")

        df = st.session_state.df
        if df.empty:
            st.warning("还没有加载样本。")
            st.caption(f"保存目标：{current_output_path().name}")
            return None
        statuses = sorted([status for status in df["status"].unique().tolist() if str(status).strip()]) or ["draft"]
        status_filter = st.multiselect("状态筛选", options=statuses, default=statuses)
        view_df = df[df["status"].isin(status_filter)] if status_filter else df
        if view_df.empty:
            st.warning("没有符合筛选条件的样本。")
            return None

        sample_ids = view_df["sample_id"].tolist()
        all_sample_ids = df["sample_id"].tolist()
        if st.session_state.selected_sample_id not in all_sample_ids:
            st.session_state.selected_sample_id = all_sample_ids[0]

        select_sample_ids = sample_ids
        if st.session_state.selected_sample_id not in select_sample_ids:
            select_sample_ids = [st.session_state.selected_sample_id, *sample_ids]
        if st.session_state.get("sidebar_choice_id") not in select_sample_ids:
            st.session_state.sidebar_choice_id = st.session_state.selected_sample_id

        st.selectbox(
            "选择样本",
            select_sample_ids,
            format_func=lambda sample_id: sample_label_by_id(df, sample_id),
            key="sidebar_choice_id",
            on_change=sync_sidebar_choice,
        )

        if st.button("保存全部到本次时间戳 CSV", use_container_width=True):
            save_data(st.session_state.df)
            st.success(f"已保存：{current_output_path().name}")

        loaded_name = st.session_state.get("loaded_display_name") or (
            Path(str(st.session_state.loaded_path)).name if st.session_state.get("loaded_path") else "none"
        )
        st.caption(f"当前数据：{loaded_name}")
        st.caption(f"保存目标：{current_output_path().name}")
        st.caption(f"当前可见样本：{len(view_df)} / 全部样本：{len(df)}")
        return view_df

def add_selection_column(df: pd.DataFrame, linked_ids: set[str] | None = None) -> pd.DataFrame:
    out = df.copy()
    if "_selected" not in out.columns:
        out.insert(0, "_selected", False)
    if linked_ids is not None:
        linked = out.get("id", pd.Series([""] * len(out))).apply(lambda value: "*" if str(value) in linked_ids else "")
        out.insert(1, "_linked", linked)
    return out


def editor_height(row_count: int) -> int:
    rows = min(max(row_count, 2), 5)
    return 42 + rows * 34


def editor_from_json(
    row: pd.Series,
    col: str,
    columns: list[str],
    key: str,
    linked_ids: set[str] | None = None,
) -> pd.DataFrame:
    sample_id = str(row.get("sample_id", ""))
    draft_key = f"{sample_id}::{col}"
    if draft_key in st.session_state.editor_drafts:
        df = st.session_state.editor_drafts[draft_key].copy()
    else:
        records = parse_json_cell(row.get(col, "[]"))
        df = pd.DataFrame(records)
    for column in columns:
        if column not in df.columns:
            df[column] = ""
    df = stringify_nested_cells(df[columns])
    df = add_selection_column(df, linked_ids)
    column_config = {"_selected": st.column_config.CheckboxColumn("选中", width="small")}
    if linked_ids is not None:
        column_config["_linked"] = st.column_config.TextColumn("关联", width="small", disabled=True)
    for text_col in ["mention", "normalized_name", "source", "target", "members", "result"]:
        if text_col in df.columns:
            column_config[text_col] = st.column_config.TextColumn(text_col, width="medium")
    if "evidence" in df.columns:
        column_config["evidence"] = st.column_config.TextColumn("evidence", width=720)
    edited = st.data_editor(
        df,
        key=key,
        num_rows="dynamic",
        width="stretch",
        height=editor_height(len(df)),
        hide_index=True,
        column_config=column_config,
    )
    st.markdown('<div class="table-bottom-gap"></div>', unsafe_allow_html=True)
    st.session_state.editor_drafts[draft_key] = edited.copy()
    return edited


def selected_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if "_selected" not in df.columns:
        return []
    mask = df["_selected"].apply(lambda value: str(value).lower() in {"true", "1", "yes"})
    return rows_without_transient(df[mask])


def first_selected_record(df: pd.DataFrame) -> dict[str, Any] | None:
    records = selected_records(df)
    return records[0] if records else None


def evidence_records(value: Any) -> list[dict[str, Any]]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    parsed = parse_editor_cell("evidence", value)
    if isinstance(parsed, list):
        rows = []
        for item in parsed:
            if isinstance(item, dict):
                rows.append(
                    {
                        "chunk_id": item.get("chunk_id", ""),
                        "text_field": item.get("text_field", ""),
                        "start": format_position_cell(item.get("start")),
                        "end": format_position_cell(item.get("end")),
                        "text": item.get("text", ""),
                    }
                )
        return rows
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return []
    return [{"chunk_id": "", "text_field": "", "start": "", "end": "", "text": text}]


def render_evidence_preview(title: str, df: pd.DataFrame) -> None:
    selected = first_selected_record(df)
    if not selected:
        st.caption(f"{title}: 勾选一行后在这里查看 evidence 解析。")
        return
    rows = evidence_records(selected.get("evidence", ""))
    if not rows:
        st.caption(f"{title}: 选中行没有 evidence。")
        return
    st.markdown(f'<div class="evidence-preview-title">{title} evidence 解析</div>', unsafe_allow_html=True)
    st.dataframe(
        pd.DataFrame(rows, columns=["chunk_id", "text_field", "start", "end", "text"]),
        use_container_width=True,
        hide_index=True,
        height=min(260, 42 + max(len(rows), 1) * 42),
    )


def entity_options(entities_df: pd.DataFrame, context_entities: pd.DataFrame | None = None, target_entities: pd.DataFrame | None = None) -> list[dict[str, Any]]:
    entities = []
    if entities_df is not None and not entities_df.empty:
        for record in rows_without_transient(entities_df):
            entity_id = str(record.get("id", "")).strip()
            if entity_id:
                entities.append(record)
        return entities
    for scope, df in [("context", context_entities), ("target", target_entities)]:
        if df is None:
            continue
        for record in rows_without_transient(df):
            entity_id = str(record.get("id", "")).strip()
            if not entity_id:
                continue
            record["scope"] = scope
            entities.append(record)
    return entities


def entity_by_id(entities: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(entity.get("id")): entity for entity in entities if entity.get("id")}


def collect_linked_entity_ids(relations: pd.DataFrame, logic_groups: pd.DataFrame) -> set[str]:
    linked: set[str] = set()
    relation = first_selected_record(relations)
    if relation:
        for key in ["source", "target"]:
            if relation.get(key):
                linked.add(str(relation[key]))
    logic = first_selected_record(logic_groups)
    if logic:
        for member in parse_editor_cell("members", logic.get("members", [])):
            linked.add(str(member))
        if logic.get("result"):
            linked.add(str(logic["result"]))
    return linked


def add_entity_highlights(highlights: list[dict[str, Any]], entities: dict[str, dict[str, Any]], ids: set[str]) -> None:
    for entity_id in ids:
        entity = entities.get(str(entity_id))
        if not entity:
            continue
        for evidence in parse_editor_cell("evidence", entity.get("evidence", [])):
            if not isinstance(evidence, dict):
                continue
            try:
                start = parse_position(evidence.get("start"))
                end = parse_position(evidence.get("end"))
            except ValueError:
                continue
            field = normalize_text_field(evidence.get("text_field"), entity.get("scope"))
            if end > start:
                highlights.append({"field": field, "start": start, "end": end, "kind": "entity"})
        field = normalize_text_field(entity.get("text_field"), entity.get("scope"))
        try:
            start = parse_position(entity.get("start"))
            end = parse_position(entity.get("end"))
        except ValueError:
            continue
        if end > start:
            highlights.append({"field": field, "start": start, "end": end, "kind": "entity"})


def parse_position(value: Any) -> int:
    text = str(value).strip()
    if not text:
        raise ValueError("empty position")
    return int(float(text))


def normalize_text_field(value: Any, scope: Any = None) -> str:
    text = str(value or "").strip().lower()
    if text in {"context", "context_text", "previous", "prev"}:
        return "context_text"
    if text in {"target", "target_text", "text", "current"}:
        return "text"
    return "context_text" if scope == "context" else "text"


def add_evidence_highlights(highlights: list[dict[str, Any]], evidence: str, context_text: str, text: str) -> None:
    evidence_items = parse_editor_cell("evidence", evidence)
    if isinstance(evidence_items, list):
        for item in evidence_items:
            if not isinstance(item, dict):
                continue
            try:
                start = parse_position(item.get("start"))
                end = parse_position(item.get("end"))
            except ValueError:
                continue
            field = normalize_text_field(item.get("text_field"))
            if end > start:
                highlights.append({"field": field, "start": start, "end": end, "kind": "evidence"})
        return
    evidence = str(evidence or "").strip()
    if not evidence:
        return
    for field, source in [("context_text", context_text), ("text", text)]:
        pos = source.find(evidence)
        if pos >= 0:
            highlights.append({"field": field, "start": pos, "end": pos + len(evidence), "kind": "evidence"})


def collect_highlights(
    entities_df: pd.DataFrame,
    context_entities: pd.DataFrame,
    target_entities: pd.DataFrame,
    relations: pd.DataFrame,
    logic_groups: pd.DataFrame,
    entities: dict[str, dict[str, Any]],
    context_text: str,
    text: str,
) -> list[dict[str, Any]]:
    highlights: list[dict[str, Any]] = []
    ids: set[str] = set()
    selected_entities = []
    for entity in selected_records(entities_df):
        selected_entities.append(entity)
    for entity in selected_records(context_entities):
        entity["scope"] = "context"
        selected_entities.append(entity)
    for entity in selected_records(target_entities):
        entity["scope"] = "target"
        selected_entities.append(entity)
    for entity in selected_entities:
        if entity.get("id"):
            ids.add(str(entity["id"]))

    relation = first_selected_record(relations)
    if relation:
        ids.update(str(relation.get(k)) for k in ["source", "target"] if relation.get(k))
        add_evidence_highlights(highlights, str(relation.get("evidence", "")), context_text, text)

    logic = first_selected_record(logic_groups)
    if logic:
        ids.update(str(x) for x in parse_editor_cell("members", logic.get("members", [])))
        if logic.get("result"):
            ids.add(str(logic["result"]))
        add_evidence_highlights(highlights, str(logic.get("evidence", "")), context_text, text)

    add_entity_highlights(highlights, entities, ids)
    return highlights


def highlighted_html(source: str, field: str, highlights: list[dict[str, Any]]) -> str:
    spans = []
    for h in highlights:
        if h.get("field") != field:
            continue
        try:
            start = max(0, int(h["start"]))
            end = min(len(source), int(h["end"]))
        except (TypeError, ValueError):
            continue
        if end > start:
            spans.append((start, end, h.get("kind", "entity")))
    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    pieces = []
    cursor = 0
    for start, end, kind in spans:
        if start < cursor:
            continue
        pieces.append(html.escape(source[cursor:start]))
        color = "#fde68a" if kind == "entity" else "#bfdbfe"
        pieces.append(f"<mark style='background:{color};padding:1px 2px;border-radius:3px'>{html.escape(source[start:end])}</mark>")
        cursor = end
    pieces.append(html.escape(source[cursor:]))
    return "".join(pieces)


def render_text_block(title: str, text: str, field: str, highlights: list[dict[str, Any]], chunk_id: str) -> None:
    if not text:
        st.markdown(f"**{title}**")
        st.info("该样本没有上下文 chunk。")
        return
    body = highlighted_html(str(text), field, highlights)
    component_height = 720
    chunk_id_json = json.dumps(str(chunk_id or ""), ensure_ascii=False)
    field_json = json.dumps(field, ensure_ascii=False)
    components.html(
        f"""
        <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;">
          <div style="font-weight:600;margin:8px 0 6px;color:rgb(49, 51, 63);font-size:14px;">{html.escape(title)}</div>
          <div id="{field}-tip" style="
            display:inline-block;
            font-size:13px;
            color:#fff;
            background:#111827;
            border-radius:6px;
            padding:5px 9px;
            margin-bottom:8px;
            box-shadow:0 4px 10px rgba(0,0,0,.16);
          ">
            Select text to show start / end
          </div>
          <button id="{field}-copy" style="
            margin-left:8px;
            border:1px solid #d1d5db;
            border-radius:6px;
            background:#fff;
            color:#111827;
            padding:5px 9px;
            cursor:pointer;
          ">Copy evidence</button>
          <div id="{field}-text" style="
            white-space:pre-wrap;
            word-break:break-word;
            user-select:text;
            cursor:text;
            color:rgb(49, 51, 63);
            font-size:14px;
            line-height:1.6;
            border:1px solid #d1d5db;
            border-radius:6px;
            padding:13px;
            background:#fafafa;
            height:630px;
            overflow-y:auto;
            overflow-x:hidden;
          ">{body}</div>
        </div>
        <script>
        const box = document.getElementById("{field}-text");
        const tip = document.getElementById("{field}-tip");
        const copyBtn = document.getElementById("{field}-copy");
        function charOffset(root, node, offset) {{
          const range = document.createRange();
          range.selectNodeContents(root);
          range.setEnd(node, offset);
          return range.toString().length;
        }}
        box.addEventListener("mouseup", () => {{
          const sel = window.getSelection();
          if (!sel || sel.rangeCount === 0 || sel.toString().length === 0) return;
          const range = sel.getRangeAt(0);
          if (!box.contains(range.startContainer) || !box.contains(range.endContainer)) return;
          const start = Math.trunc(charOffset(box, range.startContainer, range.startOffset));
          const end = Math.trunc(charOffset(box, range.endContainer, range.endOffset));
          tip.dataset.start = String(start);
          tip.dataset.end = String(end);
          tip.dataset.text = sel.toString();
          tip.textContent = `start=${{start}}  end=${{end}}  length=${{end - start}}`;
        }});
        copyBtn.addEventListener("click", async () => {{
          const start = tip.dataset.start || "";
          const end = tip.dataset.end || "";
          const selectedText = tip.dataset.text || "";
          if (!selectedText) {{
            tip.textContent = "Select text first";
            return;
          }}
          const token = [{chunk_id_json}, {field_json}, start, end, selectedText].join("::");
          try {{
            await navigator.clipboard.writeText(token);
            tip.textContent = "Copied evidence";
          }} catch (e) {{
            tip.textContent = token;
          }}
        }});
        </script>
        """,
        height=component_height,
        scrolling=False,
    )

def render_position_helper(context_text: str, text: str) -> None:
    with st.expander("start / end 辅助定位", expanded=False):
        source_name = st.radio("定位范围", ["text", "context_text"], horizontal=True)
        source = text if source_name == "text" else context_text
        snippet = st.text_input("输入要查找的原文片段")
        if snippet:
            start = source.find(snippet)
            if start >= 0:
                st.success(f"start={start}, end={start + len(snippet)}")
            else:
                st.warning("没有找到完全一致的片段。")


def snapshot_from_widgets(
    status: str,
    notes: str,
    entities: pd.DataFrame,
    context_entities: pd.DataFrame,
    target_entities: pd.DataFrame,
    relations: pd.DataFrame,
    logic_groups: pd.DataFrame,
) -> dict[str, Any]:
    return {
        "status": status,
        "reviewer_notes": notes,
        "entities_json": rows_without_transient(entities),
        "context_entities_json": rows_without_transient(context_entities),
        "target_entities_json": rows_without_transient(target_entities),
        "relations_json": rows_without_transient(relations),
        "logic_groups_json": rows_without_transient(logic_groups),
    }


def current_saved_snapshot(row: pd.Series) -> dict[str, Any]:
    return {
        "status": row.get("status", "draft") or "draft",
        "reviewer_notes": row.get("reviewer_notes", ""),
        "entities_json": parse_json_cell(row.get("entities_json", "[]")),
        "context_entities_json": parse_json_cell(row.get("context_entities_json", "[]")),
        "target_entities_json": parse_json_cell(row.get("target_entities_json", "[]")),
        "relations_json": parse_json_cell(row.get("relations_json", "[]")),
        "logic_groups_json": parse_json_cell(row.get("logic_groups_json", "[]")),
    }


def apply_snapshot(idx: int, snapshot: dict[str, Any]) -> None:
    df = st.session_state.df
    df.loc[idx, "status"] = snapshot["status"]
    df.loc[idx, "reviewer_notes"] = snapshot["reviewer_notes"]
    for col in JSON_COLUMNS:
        df.loc[idx, col] = json.dumps(snapshot[col], ensure_ascii=False)
    save_data(df)


def push_undo(sample_id: str, snapshot: dict[str, Any]) -> None:
    stack = st.session_state.undo_stack.setdefault(sample_id, [])
    stack.append(snapshot)
    if len(stack) > 30:
        del stack[0]
    st.session_state.redo_stack[sample_id] = []


def autosave_if_changed(idx: int, sample_id: str, snapshot: dict[str, Any]) -> None:
    digest = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    if st.session_state.last_saved_snapshot.get(sample_id) == digest:
        return
    if st.session_state.last_saved_snapshot.get(sample_id) is None:
        st.session_state.last_saved_snapshot[sample_id] = digest
        return
    previous = current_saved_snapshot(st.session_state.df.loc[idx])
    push_undo(sample_id, previous)
    apply_snapshot(idx, snapshot)
    st.session_state.last_saved_snapshot[sample_id] = digest


def persist_current_sample(
    idx: int,
    status: str,
    notes: str,
    entities: pd.DataFrame,
    context_entities: pd.DataFrame,
    target_entities: pd.DataFrame,
    relations: pd.DataFrame,
    logic_groups: pd.DataFrame,
) -> None:
    snapshot = snapshot_from_widgets(status, notes, entities, context_entities, target_entities, relations, logic_groups)
    autosave_if_changed(idx, st.session_state.df.loc[idx, "sample_id"], snapshot)


def bump_editor_version(sample_id: str) -> None:
    versions = st.session_state.editor_versions
    versions[sample_id] = versions.get(sample_id, 0) + 1


def clear_editor_drafts(sample_id: str) -> None:
    prefix = f"{sample_id}::"
    for key in list(st.session_state.editor_drafts.keys()):
        if str(key).startswith(prefix):
            st.session_state.editor_drafts.pop(key, None)


def editor_key(sample_id: str, name: str) -> str:
    return f"{name}_{sample_id}_{st.session_state.editor_versions.get(sample_id, 0)}"


def render_status_badge(status: str) -> None:
    cls = f"status-{status}" if status in STATUS_OPTIONS else "status-draft"
    st.markdown(f'<div class="status-badge {cls}">{html.escape(status)}</div>', unsafe_allow_html=True)


def render_relation_endpoint_editor(
    relations: pd.DataFrame,
    entities_df: pd.DataFrame,
    context_entities: pd.DataFrame,
    target_entities: pd.DataFrame,
    sample_id: str,
) -> pd.DataFrame:
    relation = first_selected_record(relations)
    if not relation:
        st.info("勾选一条关系后，可以在这里重新选择 source / target。")
        return relations

    entities = entity_options(entities_df, context_entities, target_entities)
    if not entities:
        st.warning("当前没有实体，无法修改关系两侧实体。")
        return relations

    labels = []
    id_to_label = {}
    for entity in entities:
        entity_id = str(entity.get("id", ""))
        label = f"{entity_id} | {entity.get('normalized_name') or entity.get('mention', '')} | {entity.get('type', '')}"
        labels.append(label)
        id_to_label[entity_id] = label

    source_id = str(relation.get("source", ""))
    target_id = str(relation.get("target", ""))
    st.markdown("**修改选中关系两端实体**")
    c1, c2, c3 = st.columns([1, 1, 0.55])
    source_label = c1.selectbox(
        "source",
        labels,
        index=labels.index(id_to_label[source_id]) if source_id in id_to_label else 0,
        key=f"source_select_{sample_id}",
    )
    target_label = c2.selectbox(
        "target",
        labels,
        index=labels.index(id_to_label[target_id]) if target_id in id_to_label else 0,
        key=f"target_select_{sample_id}",
    )
    if c3.button("应用到选中关系", use_container_width=True):
        updated = relations.copy()
        selected_idx = updated.index[updated["_selected"].apply(lambda value: str(value).lower() in {"true", "1", "yes"})]
        if len(selected_idx):
            updated.loc[selected_idx[0], "source"] = source_label.split(" | ", 1)[0]
            updated.loc[selected_idx[0], "target"] = target_label.split(" | ", 1)[0]
            st.session_state[f"pending_relations_{sample_id}"] = updated
            bump_editor_version(sample_id)
            st.rerun()
    return relations


def parse_evidence_token(token: str) -> dict[str, Any] | None:
    parts = str(token or "").strip().split("::", 4)
    if len(parts) != 5:
        return None
    try:
        start = parse_position(parts[2])
        end = parse_position(parts[3])
    except ValueError:
        return None
    return {
        "chunk_id": parts[0],
        "text_field": normalize_text_field(parts[1]),
        "start": start,
        "end": end,
        "text": parts[4],
    }


def next_entity_id(entities_df: pd.DataFrame) -> str:
    existing = {str(row.get("id", "")) for row in rows_without_transient(entities_df)}
    index = 1
    while f"E{index}" in existing:
        index += 1
    return f"E{index}"


def append_evidence_to_row(df: pd.DataFrame, row_index: Any, evidence: dict[str, Any]) -> pd.DataFrame:
    updated = df.copy()
    current = []
    if "evidence" in updated.columns:
        current = evidence_records(updated.at[row_index, "evidence"])
    current.append(evidence)
    updated.at[row_index, "evidence"] = json.dumps(current, ensure_ascii=False)
    return updated


def render_evidence_quick_add(sample_id: str, entities_df: pd.DataFrame, relations: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    with st.expander("Evidence quick add", expanded=False):
        st.caption("Select text on the left, click Copy evidence, paste the token here, then apply it to a selected row.")
        token = st.text_area(
            "Evidence token",
            key=f"evidence_token_{sample_id}",
            height=72,
            placeholder="chunk_id::text_field::start::end::text",
        )
        target = st.radio("Apply to", ["selected entity", "new entity", "selected relation"], horizontal=True, key=f"evidence_target_{sample_id}")

        new_type = ""
        new_mention = ""
        new_normalized = ""
        if target == "new entity":
            c1, c2, c3 = st.columns([1, 1, 1])
            new_mention = c1.text_input("mention", key=f"new_entity_mention_{sample_id}")
            new_type = c2.selectbox("type", ENTITY_TYPES, key=f"new_entity_type_{sample_id}")
            new_normalized = c3.text_input("normalized_name", key=f"new_entity_norm_{sample_id}")

        if st.button("Apply evidence", use_container_width=True, key=f"apply_evidence_{sample_id}"):
            evidence = parse_evidence_token(token)
            if evidence is None:
                st.error("Evidence token format should be: chunk_id::text_field::start::end::text")
                return entities_df, relations

            if target == "selected entity":
                selected_idx = entities_df.index[
                    entities_df["_selected"].apply(lambda value: str(value).lower() in {"true", "1", "yes"})
                ]
                if not len(selected_idx):
                    st.error("Select one entity row first.")
                    return entities_df, relations
                st.session_state[f"pending_entities_{sample_id}"] = append_evidence_to_row(entities_df, selected_idx[0], evidence)
                bump_editor_version(sample_id)
                st.rerun()

            if target == "new entity":
                mention = new_mention.strip() or evidence["text"]
                normalized = new_normalized.strip() or mention
                rows = rows_without_transient(entities_df)
                rows.append(
                    {
                        "id": next_entity_id(entities_df),
                        "mention": mention,
                        "type": new_type or "故障事件",
                        "normalized_name": normalized,
                        "evidence": [evidence],
                    }
                )
                st.session_state[f"pending_entities_{sample_id}"] = pd.DataFrame(rows)
                bump_editor_version(sample_id)
                st.rerun()

            if target == "selected relation":
                selected_idx = relations.index[
                    relations["_selected"].apply(lambda value: str(value).lower() in {"true", "1", "yes"})
                ]
                if not len(selected_idx):
                    st.error("Select one relation row first.")
                    return entities_df, relations
                st.session_state[f"pending_relations_{sample_id}"] = append_evidence_to_row(relations, selected_idx[0], evidence)
                bump_editor_version(sample_id)
                st.rerun()

    return entities_df, relations


def main() -> None:
    st.set_page_config(page_title="标注审核工具原型", layout="wide")
    inject_styles()
    ensure_session_state()
    scroll_to_top_if_needed()

    st.title("标注审核工具原型")
    st.caption("读取 AI 草稿 CSV，编辑统一实体、关系和逻辑组，并自动保存到 reviewed CSV。")
    if st.session_state.get("last_save_warning"):
        st.warning(st.session_state.last_save_warning)

    view_df = render_sidebar()
    if view_df is None:
        return

    df = st.session_state.df
    all_sample_ids = df["sample_id"].tolist()
    if st.session_state.selected_sample_id not in all_sample_ids:
        st.session_state.selected_sample_id = all_sample_ids[0]
    idx = df.index[df["sample_id"] == st.session_state.selected_sample_id][0]
    row = df.loc[idx].copy()
    sample_id = row["sample_id"]

    left, right = st.columns([0.72, 2.28], gap="large")

    with right:
        st.subheader("人工审核")
        st.markdown('<div class="status-title">审核状态</div>', unsafe_allow_html=True)
        status_version = st.session_state.editor_versions.get(sample_id, 0)
        status = st.selectbox(
            "审核状态",
            STATUS_OPTIONS,
            index=STATUS_OPTIONS.index(row.get("status", "draft")) if row.get("status", "draft") in STATUS_OPTIONS else 0,
            label_visibility="collapsed",
            key=f"status_{sample_id}_{status_version}",
        )
        render_status_badge(status)
        notes = st.text_area("人工备注", value=row.get("reviewer_notes", ""), height=76, key=f"notes_{sample_id}_{status_version}")

        undo_col, redo_col = st.columns(2)
        if undo_col.button("撤销", use_container_width=True):
            stack = st.session_state.undo_stack.get(sample_id, [])
            if stack:
                current = current_saved_snapshot(st.session_state.df.loc[idx])
                st.session_state.redo_stack.setdefault(sample_id, []).append(current)
                apply_snapshot(idx, stack.pop())
                clear_editor_drafts(sample_id)
                bump_editor_version(sample_id)
                st.rerun()
        if redo_col.button("重做", use_container_width=True):
            stack = st.session_state.redo_stack.get(sample_id, [])
            if stack:
                current = current_saved_snapshot(st.session_state.df.loc[idx])
                st.session_state.undo_stack.setdefault(sample_id, []).append(current)
                apply_snapshot(idx, stack.pop())
                clear_editor_drafts(sample_id)
                bump_editor_version(sample_id)
                st.rerun()

        linked_ids = set(st.session_state.linked_entity_ids.get(sample_id, []))
        st.markdown("**实体 entities**")
        st.markdown('<div class="linked-help">实体不再单独保存 start/end；原文位置统一写在 evidence 数组中。勾选实体行会高亮所有 evidence。</div>', unsafe_allow_html=True)
        pending_entities_key = f"pending_entities_{sample_id}"
        if pending_entities_key in st.session_state:
            row["entities_json"] = dump_json_cell(st.session_state.pop(pending_entities_key))
            st.session_state.editor_drafts.pop(f"{sample_id}::entities_json", None)
        entities_df = editor_from_json(
            row,
            "entities_json",
            ["id", "mention", "type", "normalized_name", "evidence"],
            editor_key(sample_id, "entities"),
            linked_ids=linked_ids,
        )
        render_evidence_preview("实体", entities_df)
        context_entities = pd.DataFrame()
        target_entities = pd.DataFrame()

        pending_key = f"pending_relations_{sample_id}"
        if pending_key in st.session_state:
            row["relations_json"] = dump_json_cell(st.session_state.pop(pending_key))
            st.session_state.editor_drafts.pop(f"{sample_id}::relations_json", None)
        st.markdown("**关系 relations**")
        relations = editor_from_json(
            row,
            "relations_json",
            ["id", "source", "relation_type", "target", "cross_chunk", "involved_chunk_ids", "evidence", "polarity", "certainty"],
            editor_key(sample_id, "relations"),
        )
        render_evidence_preview("关系", relations)
        relations = render_relation_endpoint_editor(relations, entities_df, context_entities, target_entities, sample_id)
        entities_df, relations = render_evidence_quick_add(sample_id, entities_df, relations)

        st.markdown("**逻辑组 logic_groups**")
        logic_groups = editor_from_json(
            row,
            "logic_groups_json",
            ["id", "logic_type", "members", "result", "relation_type", "involved_chunk_ids", "evidence"],
            editor_key(sample_id, "logic"),
        )
        render_evidence_preview("逻辑组", logic_groups)

        new_linked_ids = collect_linked_entity_ids(relations, logic_groups)
        if set(st.session_state.linked_entity_ids.get(sample_id, [])) != new_linked_ids:
            st.session_state.linked_entity_ids[sample_id] = sorted(new_linked_ids)

        actions = st.columns(3)
        if actions[0].button("保存当前样本", use_container_width=True):
            persist_current_sample(idx, status, notes, entities_df, context_entities, target_entities, relations, logic_groups)
            st.success(f"当前样本已保存到 {current_output_path().name}")
        if actions[1].button("标注为已审核", use_container_width=True):
            persist_current_sample(idx, "reviewed", notes, entities_df, context_entities, target_entities, relations, logic_groups)
            bump_editor_version(sample_id)
            st.session_state.scroll_to_top = True
            st.success("已标记为 reviewed 并保存。")
            st.rerun()
        if actions[2].button("重新加载当前数据", use_container_width=True):
            reload_current_dataset()
            st.rerun()

        snapshot = snapshot_from_widgets(status, notes, entities_df, context_entities, target_entities, relations, logic_groups)
        autosave_if_changed(idx, sample_id, snapshot)
        st.caption(f"自动保存文件：{current_output_path().name}")

        st.markdown('<div class="nav-button-note">切换样本时会自动保存当前修改。</div>', unsafe_allow_html=True)
        nav_cols = st.columns(2)
        current_pos = get_sample_position(all_sample_ids, sample_id)
        if nav_cols[0].button("↑\n上一个样本", use_container_width=True, disabled=current_pos <= 0):
            persist_current_sample(idx, status, notes, entities_df, context_entities, target_entities, relations, logic_groups)
            st.session_state.selected_sample_id = all_sample_ids[current_pos - 1]
            st.session_state.scroll_to_top = True
            st.rerun()
        if nav_cols[1].button("↓\n下一个样本", use_container_width=True, disabled=current_pos >= len(all_sample_ids) - 1):
            persist_current_sample(idx, status, notes, entities_df, context_entities, target_entities, relations, logic_groups)
            st.session_state.selected_sample_id = all_sample_ids[current_pos + 1]
            st.session_state.scroll_to_top = True
            st.rerun()

    entities = entity_options(entities_df, context_entities, target_entities)
    highlights = collect_highlights(
        entities_df,
        context_entities,
        target_entities,
        relations,
        logic_groups,
        entity_by_id(entities),
        row.get("context_text", ""),
        row.get("text", ""),
    )

    with left:
        st.subheader("原文")
        st.text_input("sample_id", value=row["sample_id"], disabled=True)
        st.text_input("file_id", value=row.get("file_id", ""), disabled=True)
        st.text_input("chapter_id", value=row.get("chapter_id", ""), disabled=True)
        st.text_input("target_chunk_id", value=row.get("target_chunk_id", ""), disabled=True)
        render_position_helper(row.get("context_text", ""), row.get("text", ""))
        render_text_block("context_text", row.get("context_text", ""), "context_text", highlights, row.get("context_chunk_id", ""))
        render_text_block("text", row.get("text", ""), "text", highlights, row.get("target_chunk_id", ""))

    st.divider()
    st.caption(f"可从左侧选择或上传 CSV；本次审核保存到：{current_output_path().name}")


if __name__ == "__main__":
    main()
