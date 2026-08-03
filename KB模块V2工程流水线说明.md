# KB 模块 V2 工程流水线说明

本文档说明当前 KB 模块的 V2 构建链路。V2 的目标是把“文档解析、chunk 入库、LLM 抽取、文件内聚类、跨文件聚类、MongoDB/Neo4j 导入”收敛为一条可运行的工程流水线。

## 1. 主入口

命令行入口：

```text
kb_pipeline_v2.py
```

FastAPI 普通文档构建入口：

```text
POST /api/kb/jobs/run
POST /api/kb/jobs/run-upload
```

这两个 FastAPI 入口已改为调用 `kb_pipeline_v2.py`。旧的 `run.py -> extract_entities.py -> extract_relations.py -> import_relations_to_neo4j.py` 链路不再作为普通文档构建主流程。

## 2. V2 流程

1. 复用当前 KB 模块已有的 OCR、Markdown 清洗和 chunk 切分能力。
2. 将原始 chunks、file、file_version 信息写入 MongoDB。
3. 使用统一 prompt 调用 LLM，输出 `[ENTITY] / [RELATION] / [LOGIC_GROUP]` 格式。
4. 将同一文件所有窗口的模型输出解析为中间 CSV 和 raw JSONL。
5. 调用 `entity_clustering/cluster_entities.py` 做文件内实体消歧聚类。
6. 调用 `cross_file_entity_clustering.py` 与当前 Neo4j 中已有实体做跨文件保守聚类。
7. 将 Mention、EntityCluster、聚类后关系和 `top_event_catalog` 写入 Neo4j/MongoDB。

## 3. 产物命名

输出目录：

```text
output\<file_id>\<file_version_id>\
```

主要产物：

```text
<file_id>_chunks.json
<file_id>.csv
<file_id>_raw_llm.jsonl
entity_clustering\cluster_intermediate.json
cross_file_diagnostics.jsonl
<file_id>_kb_pipeline_summary.json
```

说明：

- `<file_id>.csv`：LLM 结构化抽取后的样本级中间产物，不再使用 `_annotations` 后缀。
- `<file_id>_raw_llm.jsonl`：每次 LLM 调用的 prompt、原始输出、窗口信息和解析结果。
- `cluster_intermediate.json`：文件内实体聚类后的完整中间产物。
- `cross_file_diagnostics.jsonl`：跨文件聚类候选与决策诊断。
- `<file_id>_kb_pipeline_summary.json`：本次流水线摘要。

## 4. 环境变量

在 `FTA-KB/.env` 中至少配置：

```env
MONGO_URI=mongodb://localhost:27017/
MONGO_DB_NAME=smart-fta-test

NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=你的Neo4j密码
NEO4J_DATABASE=neo4j

LLM_API_KEY=你的LLM Key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

EMBEDDING_API_KEY=你的Embedding Key
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
```

FastAPI 调用 V2 时还会读取以下可选变量：

```env
KB_V2_LLM_WORKERS=1
KB_V2_EMBEDDING_BACKEND=none
KB_V2_REFINEMENT_MERGE_MODE=single
```

如果要使用 DashScope embedding，可设置：

```env
KB_V2_EMBEDDING_BACKEND=openai-compatible
```

## 5. 命令行运行

在 `FTA-KB` 目录下运行：

```powershell
.\.venv\Scripts\python.exe kb_pipeline_v2.py `
  --env-file .env `
  --input-file "E:\path\to\manual.pdf" `
  --output-dir output `
  --file-id file_manual_demo `
  --file-version-id file_manual_demo_v1 `
  --file-name "示例设备手册.pdf" `
  --source-type manual_document `
  --file-format pdf `
  --chunk-size 800 `
  --workers 2 `
  --embedding-backend openai-compatible
```

Markdown、txt、csv 等文本文件可跳过 MinerU：

```powershell
.\.venv\Scripts\python.exe kb_pipeline_v2.py `
  --env-file .env `
  --input-file "E:\path\to\manual.md" `
  --output-dir output `
  --file-id file_manual_demo `
  --file-version-id file_manual_demo_v1 `
  --file-name "示例设备手册.md" `
  --source-type manual_document `
  --file-format md `
  --skip-mineru `
  --embedding-backend openai-compatible
```

## 6. FastAPI 使用

启动服务：

```powershell
uvicorn main:app --host 0.0.0.0 --port 8020 --reload
```

普通文档上传走：

```text
POST /api/kb/jobs/run-upload
```

服务会：

1. 保存上传文件；
2. 预留稳定的 `file_id` 和 `file_version_id`；
3. 调用 `kb_pipeline_v2.py`；
4. 将 chunks、聚类实体、关系和 top event catalog 写入 MongoDB/Neo4j；
5. 在 job 结果中返回 V2 产物路径。

### 前端 job 字段兼容

`GET /api/kb/jobs/{job_id}` 仍保持旧前端需要的字段：

```text
job_id
status
stage
progress
message
error
stdout
sync_status
sync_response.file
file_id
file_version_id
version_no
uploaded_file_path
uploaded_file_name
artifacts
```

其中 `stage` 继续使用前端已识别的稳定枚举：

```text
prepare -> parse -> chunk -> entity -> relation -> syncing -> success/failed
```

V2 内部的 LLM 抽取、实体聚类、跨文件聚类会映射到旧阶段：

- LLM 抽取：`entity`
- 文件内实体聚类与关系聚合：`relation`
- 跨文件聚类、Neo4j/MongoDB 导入：`syncing`

`kb_pipeline_v2.py` 会向 stdout 打印 `KB_STAGE=<stage>|<message>`，FastAPI 实时读取 stdout 并更新 job。LLM 抽取阶段的 `[i/n]` 日志会被映射为 55%-73% 的实时进度。失败时，FastAPI 会把子进程 stdout 末尾摘要写入 `error` 和 `message`，前端可直接展示。

## 7. 旧文件暂存

以下旧流程文件已移动到：

```text
legacy_v1_pipeline\
```

包括：

```text
extract_entities.py
extract_relations.py
generate_prompt_relation.py
llm_caller_relation.py
import_relations_to_neo4j.py
```

这些文件暂时保留，便于回看旧实现；普通文档构建不再依赖它们。`run.py` 仍保留在根目录，因为 V2 继续复用它的 OCR、清洗和 chunk 切分能力。
