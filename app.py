import html
import json
from pathlib import Path

import pandas as pd
import streamlit as st


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "sample_annotations.csv"
DEFAULT_DRAFT = BASE_DIR / "ai_annotation_draft.csv"
DEFAULT_OUTPUT = BASE_DIR / "reviewed_annotations.csv"

JSON_COLUMNS = ["context_entities_json", "target_entities_json", "relations_json", "logic_groups_json"]
STATUS_OPTIONS = ["draft", "reviewing", "reviewed", "needs_fix"]
NESTED_LIST_COLUMNS = {"members", "involved_chunk_ids"}


def parse_json_cell(value):
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


def parse_editor_cell(key, value):
    if isinstance(value, (list, dict, bool, int, float)):
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


def dump_json_cell(value):
    records = value.to_dict("records") if isinstance(value, pd.DataFrame) else value
    cleaned = []
    for record in records:
        cleaned_record = {}
        for key, item in record.items():
            if pd.isna(item):
                cleaned_record[key] = ""
            else:
                cleaned_record[key] = parse_editor_cell(key, item)
        cleaned.append(cleaned_record)
    return json.dumps(cleaned, ensure_ascii=False)


def load_data(path):
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if "file_id" not in df.columns and "source_doc_id" in df.columns:
        df["file_id"] = df["source_doc_id"]
    if "chapter_id" not in df.columns:
        df["chapter_id"] = ""
    if "context_entities_json" not in df.columns:
        df["context_entities_json"] = "[]"
    if "target_entities_json" not in df.columns:
        df["target_entities_json"] = df["entities_json"] if "entities_json" in df.columns else "[]"
    for column in ["relations_json", "logic_groups_json"]:
        if column not in df.columns:
            df[column] = "[]"
    if "status" not in df.columns:
        df["status"] = "draft"
    if "reviewer_notes" not in df.columns:
        df["reviewer_notes"] = ""
    for column in JSON_COLUMNS:
        df[column] = df[column].fillna("[]")
    return df


def save_data(df, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def stringify_nested_cells(df):
    display_df = df.copy()
    for column in display_df.columns:
        display_df[column] = display_df[column].map(
            lambda value: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
        )
    return display_df


def ensure_session_state():
    if "df" not in st.session_state:
        st.session_state.df = load_data(DEFAULT_INPUT)
    if "selected_sample_id" not in st.session_state:
        st.session_state.selected_sample_id = st.session_state.df.iloc[0]["sample_id"]


def get_selected_index(df, sample_id):
    matches = df.index[df["sample_id"] == sample_id].tolist()
    return matches[0] if matches else df.index[0]


def get_sample_position(sample_ids, selected_sample_id):
    return sample_ids.index(selected_sample_id) if selected_sample_id in sample_ids else 0


def sample_label(row):
    return f"{row['sample_id']}  [{row.get('status', 'draft')}]"


def sample_label_by_id(df, sample_id):
    matches = df[df["sample_id"] == sample_id]
    if matches.empty:
        return str(sample_id)
    return sample_label(matches.iloc[0])


def sync_sidebar_choice():
    st.session_state.selected_sample_id = st.session_state.sidebar_choice_id


def render_text_block(title, text, is_context=False):
    st.markdown(f"**{title}**")
    if not text:
        st.info("该样本没有上下文 chunk。")
        return
    extra_class = " copyable-context" if is_context else ""
    safe_text = html.escape(str(text))
    st.markdown(f'<div class="copyable-text{extra_class}">{safe_text}</div>', unsafe_allow_html=True)


def persist_current_sample(idx, status, notes, context_entities, target_entities, relations, logic_groups):
    st.session_state.df.loc[idx, "status"] = status
    st.session_state.df.loc[idx, "reviewer_notes"] = notes
    st.session_state.df.loc[idx, "context_entities_json"] = dump_json_cell(context_entities)
    st.session_state.df.loc[idx, "target_entities_json"] = dump_json_cell(target_entities)
    st.session_state.df.loc[idx, "relations_json"] = dump_json_cell(relations)
    st.session_state.df.loc[idx, "logic_groups_json"] = dump_json_cell(logic_groups)
    save_data(st.session_state.df, DEFAULT_OUTPUT)


def inject_styles():
    st.markdown(
        """
        <style>
        div[data-testid="stButton"] button { min-height: 2.8rem; }
        .nav-button-note {
            color: #666;
            font-size: 0.85rem;
            margin-top: -0.25rem;
            margin-bottom: 0.5rem;
        }
        .status-title {
            font-size: 1.15rem;
            font-weight: 700;
            color: #0f4c81;
            padding: 0.55rem 0.75rem;
            margin: 0.15rem 0 0.35rem 0;
            border-left: 5px solid #1f77b4;
            background: #eef6ff;
            border-radius: 6px;
        }
        .status-badge {
            display: inline-block;
            font-size: 1.65rem;
            font-weight: 800;
            line-height: 1.2;
            padding: 0.35rem 0.8rem;
            margin: 0.1rem 0 0.65rem 0;
            border-radius: 8px;
            color: white;
            background: #6b7280;
        }
        .status-reviewed { background: #15803d; }
        .status-reviewing { background: #1d4ed8; }
        .status-needs-fix { background: #b91c1c; }
        .status-draft { background: #92400e; }
        .copyable-text {
            height: 360px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-break: break-word;
            user-select: text;
            cursor: text;
            padding: 0.85rem;
            border: 1px solid #d1d5db;
            border-radius: 6px;
            background: #fafafa;
            font-size: 0.93rem;
            line-height: 1.6;
        }
        .copyable-context {
            height: 240px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def scroll_to_top_if_needed():
    if st.session_state.pop("scroll_to_top", False):
        st.components.v1.html(
            """
            <script>
            window.parent.scrollTo({ top: 0, left: 0, behavior: "smooth" });
            </script>
            """,
            height=0,
        )


def editor_from_json(row, column, default_columns, key):
    df = pd.DataFrame(parse_json_cell(row.get(column, "[]")))
    if df.empty:
        df = pd.DataFrame(columns=default_columns)
    return st.data_editor(stringify_nested_cells(df), num_rows="dynamic", use_container_width=True, key=key)


def render_status_badge(status):
    class_name = {
        "draft": "status-draft",
        "reviewing": "status-reviewing",
        "reviewed": "status-reviewed",
        "needs_fix": "status-needs-fix",
    }.get(status, "")
    st.markdown(f'<div class="status-badge {class_name}">{status}</div>', unsafe_allow_html=True)


def load_dataset(path):
    st.session_state.df = load_data(path)
    st.session_state.selected_sample_id = st.session_state.df.iloc[0]["sample_id"]


def render_sidebar():
    with st.sidebar:
        st.header("样本")
        uploaded = st.file_uploader("导入 CSV", type=["csv"])
        if uploaded is not None:
            try:
                load_dataset(uploaded)
                st.success(f"已导入 {uploaded.name}，共 {len(st.session_state.df)} 条样本。")
            except Exception as exc:
                st.error(f"导入失败：{exc}")
                return None

        if st.button("直接加载 ai_annotation_draft.csv", use_container_width=True):
            try:
                load_dataset(DEFAULT_DRAFT)
                st.success(f"已加载 ai_annotation_draft.csv，共 {len(st.session_state.df)} 条样本。")
            except Exception as exc:
                st.error(f"加载失败：{exc}")
                return None

        df = st.session_state.df
        status_filter = st.multiselect(
            "状态筛选",
            options=sorted(df["status"].unique().tolist()),
            default=sorted(df["status"].unique().tolist()),
        )
        view_df = df[df["status"].isin(status_filter)] if status_filter else df
        if view_df.empty:
            st.warning("没有匹配的样本。")
            return None

        sample_ids = view_df["sample_id"].tolist()
        if "sidebar_choice_id" not in st.session_state:
            st.session_state.sidebar_choice_id = (
                st.session_state.selected_sample_id
                if st.session_state.selected_sample_id in sample_ids
                else sample_ids[0]
            )
        if st.session_state.sidebar_choice_id not in sample_ids:
            st.session_state.sidebar_choice_id = (
                st.session_state.selected_sample_id
                if st.session_state.selected_sample_id in sample_ids
                else sample_ids[0]
            )
        st.selectbox(
            "选择样本",
            sample_ids,
            format_func=lambda sample_id: sample_label_by_id(df, sample_id),
            key="sidebar_choice_id",
            on_change=sync_sidebar_choice,
        )

        if st.button("保存全部到 reviewed CSV", use_container_width=True):
            save_data(st.session_state.df, DEFAULT_OUTPUT)
            st.success(f"已保存：{DEFAULT_OUTPUT.name}")

        return view_df


def main():
    st.set_page_config(page_title="标注审核工具原型", layout="wide")
    inject_styles()
    ensure_session_state()
    scroll_to_top_if_needed()

    st.title("标注审核工具原型")
    st.caption("读取 AI 草稿 CSV，编辑上下文实体、当前实体、关系和逻辑组，并保存到 reviewed CSV。")

    view_df = render_sidebar()
    if view_df is None:
        return

    all_sample_ids = st.session_state.df["sample_id"].tolist()
    idx = get_selected_index(st.session_state.df, st.session_state.selected_sample_id)
    row = st.session_state.df.loc[idx].copy()

    left, right = st.columns([0.72, 2.28], gap="large")
    with left:
        st.subheader("原文")
        st.text_input("sample_id", value=row["sample_id"], disabled=True)
        st.text_input("file_id", value=row.get("file_id", ""), disabled=True)
        st.text_input("chapter_id", value=row.get("chapter_id", ""), disabled=True)
        st.text_input("target_chunk_id", value=row.get("target_chunk_id", ""), disabled=True)
        render_text_block("context_text", row.get("context_text", ""), is_context=True)
        render_text_block("text", row.get("text", ""))

    with right:
        st.subheader("人工复核")
        st.markdown('<div class="status-title">审核状态</div>', unsafe_allow_html=True)
        status = st.selectbox(
            "审核状态",
            STATUS_OPTIONS,
            index=STATUS_OPTIONS.index(row.get("status", "draft")) if row.get("status", "draft") in STATUS_OPTIONS else 0,
            label_visibility="collapsed",
        )
        render_status_badge(status)
        notes = st.text_area("人工备注", value=row.get("reviewer_notes", ""), height=70)

        st.markdown("**上下文实体 context_entities**")
        context_entities = editor_from_json(
            row,
            "context_entities_json",
            ["id", "mention", "type", "normalized_name", "chunk_id", "text_field", "start", "end", "annotation_role"],
            f"context_entities_{row['sample_id']}",
        )

        st.markdown("**当前实体 target_entities**")
        target_entities = editor_from_json(
            row,
            "target_entities_json",
            ["id", "mention", "type", "normalized_name", "chunk_id", "text_field", "start", "end"],
            f"target_entities_{row['sample_id']}",
        )

        st.markdown("**关系 relations**")
        relations = editor_from_json(
            row,
            "relations_json",
            ["id", "source", "relation_type", "target", "cross_chunk", "involved_chunk_ids", "evidence", "polarity", "certainty"],
            f"relations_{row['sample_id']}",
        )

        st.markdown("**逻辑组 logic_groups**")
        logic_groups = editor_from_json(
            row,
            "logic_groups_json",
            ["id", "logic_type", "members", "result", "relation_type", "involved_chunk_ids", "evidence"],
            f"logic_{row['sample_id']}",
        )

        actions = st.columns(3)
        if actions[0].button("保存当前样本", use_container_width=True):
            persist_current_sample(idx, status, notes, context_entities, target_entities, relations, logic_groups)
            st.success(f"当前样本已保存到 {DEFAULT_OUTPUT.name}")

        if actions[1].button("标记已审核", use_container_width=True):
            persist_current_sample(idx, "reviewed", notes, context_entities, target_entities, relations, logic_groups)
            st.success("已标记为 reviewed 并保存。")

        if actions[2].button("重载示例数据", use_container_width=True):
            load_dataset(DEFAULT_INPUT)
            st.rerun()

        st.markdown('<div class="nav-button-note">切换样本时会自动保存当前修改。</div>', unsafe_allow_html=True)
        nav_cols = st.columns(2)
        current_pos = get_sample_position(all_sample_ids, st.session_state.selected_sample_id)
        if nav_cols[0].button("↑\n上一个样本", use_container_width=True, disabled=current_pos <= 0):
            persist_current_sample(idx, status, notes, context_entities, target_entities, relations, logic_groups)
            st.session_state.selected_sample_id = all_sample_ids[current_pos - 1]
            st.session_state.scroll_to_top = True
            st.rerun()
        if nav_cols[1].button("↓\n下一个样本", use_container_width=True, disabled=current_pos >= len(all_sample_ids) - 1):
            persist_current_sample(idx, status, notes, context_entities, target_entities, relations, logic_groups)
            st.session_state.selected_sample_id = all_sample_ids[current_pos + 1]
            st.session_state.scroll_to_top = True
            st.rerun()

    st.divider()
    st.caption(f"输入文件：{DEFAULT_INPUT.name}；保存文件：{DEFAULT_OUTPUT.name}")


if __name__ == "__main__":
    main()
