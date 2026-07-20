# annotation-data-pipeline

标注数据生成与人工审核流程的独立工作区。

当前原型包含：

- `标注审核工具原型计划.md`：流程和格式说明。
- `sample_annotations.csv`：模拟标注样本。
- `app.py`：Streamlit 人工复核原型。
- `generate_ai_annotation_dataset.py`：从 MongoDB chunks 生成 AI 标注草稿 CSV。

启动方式：

```powershell
pip install -r requirements.txt
streamlit run app.py
```

修改后的结果默认保存到：

```text
reviewed_annotations.csv
```

从 MongoDB chunks 生成待审核 CSV：

```powershell
copy .env.example .env
# 修改 .env 中的 MongoDB 与 LLM 配置
python generate_ai_annotation_dataset.py --file-version-id file_xxx_v1 --limit 3
```

生成结果：

```text
ai_annotation_draft.csv
raw_ai_annotations.jsonl
```

其中 `ai_annotation_draft.csv` 可以直接在 Streamlit 页面左侧导入；`raw_ai_annotations.jsonl` 保留每次调用的 prompt、模型输出和 chunk 信息，便于追溯。

将人工审核后的 CSV 转成最终 JSONL：

```powershell
python convert_reviewed_csv_to_jsonl.py --input-csv reviewed_annotations.csv --output-jsonl final_annotations.jsonl
```
