# SmartFTA KB 模块总体实现说明（给 AI / 开发者阅读）

本文档用于让后续接手 SmartFTA 的 AI 或开发者快速理解当前 `FTA-KB` 模块的工程实现。重点说明 KB v2 流水线、LLM 抽取格式、MongoDB / Neo4j 存储口径，以及实体消歧聚类与跨文件聚类的具体策略。

## 1. 模块定位

`FTA-KB` 是 SmartFTA 的知识库构建模块，负责把用户上传的设备手册、工单、维修记录等文档转换成可供 `FTA-GNR` 使用的知识图谱与证据库。

当前主流程是 **KB v2 流水线**：

```text
文档上传
-> OCR / 文本清洗 / chunk 切分
-> chunks 写入 MongoDB
-> LLM 抽取实体、关系、逻辑组
-> 同一文件内实体消歧聚类
-> 与 Neo4j 现有实体做跨文件保守聚类
-> 写入 Neo4j 的 Mention / EntityCluster / 关系 / Chunk
-> 写入 MongoDB 的 top_event_catalog
-> 激活当前 file_version
```

普通文档构建不再使用旧的 `extract_entities.py -> extract_relations.py -> import_relations_to_neo4j.py` 链路。旧文件已暂存在 `legacy_v1_pipeline/`，只用于回看。

## 2. 主要入口文件

| 文件 | 作用 |
|---|---|
| `main.py` | FastAPI 服务入口，提供前端上传、构建任务、进度查询等接口。 |
| `kb_pipeline_v2.py` | KB v2 命令行主流水线，也是 FastAPI 构建任务实际调用的脚本。 |
| `run.py` | 复用旧流程中的 OCR、清洗、切 chunk 能力；v2 只用它完成抽取前处理。 |
| `chunk_md.py` | 将清洗后的 Markdown 切成 chunks。 |
| `import_chunks.py` / `knowledge_store.py` | 将 chunks、file、file_version 写入 MongoDB。 |
| `llm_annotation_extractor.py` | 调用 LLM，对 chunk 滑动窗口抽取 `[ENTITY] / [RELATION] / [LOGIC_GROUP]`。 |
| `entity_clustering/cluster_entities.py` | 同一文件内实体消歧聚类，生成 `cluster_intermediate.json`。 |
| `cross_file_entity_clustering.py` | 跨文件实体聚类，并将结果导入 Neo4j 和 MongoDB。 |
| `top_event_catalog_utils.py` | 为 MongoDB `top_event_catalog` 生成 `semantic_text` 和 embedding。 |
| `repair_top_event_catalog_embeddings.py` | 给旧数据库中缺失 embedding 的 `top_event_catalog` 补齐向量。 |

## 3. FastAPI 与命令行入口

FastAPI 普通文档构建入口：

```text
POST /api/kb/jobs/run
POST /api/kb/jobs/run-upload
GET  /api/kb/jobs/{job_id}
```

`main.py` 收到任务后会启动 `kb_pipeline_v2.py` 子进程，并解析 stdout 中的：

```text
KB_STAGE=<stage>|<message>
```

前端进度阶段保持旧接口兼容：

```text
prepare -> parse -> chunk -> entity -> relation -> syncing -> success/failed
```

其中：

- `parse`：OCR、Markdown 清洗、切 chunk；
- `chunk`：chunks 写入 MongoDB；
- `entity`：LLM 抽取实体、关系、逻辑组；
- `relation`：文件内实体聚类与关系聚合；
- `syncing`：跨文件聚类、Neo4j / MongoDB 导入；
- `success`：文件版本激活并完成。

命令行示例：

```powershell
.\.venv\Scripts\python.exe kb_pipeline_v2.py `
  --env-file .env `
  --input-file "E:\path\to\manual.pdf" `
  --output-dir output `
  --file-id file_demo `
  --file-version-id file_demo_v1 `
  --file-name "示例设备手册.pdf" `
  --source-type manual_document `
  --file-format pdf `
  --chunk-size 800 `
  --workers 2 `
  --embedding-backend openai-compatible
```

## 4. 环境变量

主要读取 `FTA-KB/.env`，实体聚类 embedding 也会读取 `FTA-KB/entity_clustering/.env` 并映射到通用 `EMBEDDING_*`。

核心配置：

```env
MONGO_URI=mongodb://localhost:27017/
MONGO_DB_NAME=smart-fta-test

NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=...
NEO4J_DATABASE=neo4j

LLM_API_KEY=...
LLM_BASE_URL=https://api.moonshot.cn/v1
LLM_MODEL=kimi-k2-0711-preview

EMBEDDING_API_KEY=...
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_BATCH_SIZE=10

KB_V2_LLM_WORKERS=1
KB_V2_EMBEDDING_BACKEND=openai-compatible
KB_V2_REFINEMENT_MERGE_MODE=single
```

`--embedding-backend none` 只适合冒烟测试。正式知识库构建应使用 embedding，否则实体聚类和顶事件语义匹配能力会明显下降。

## 5. 文档解析与 chunk 入库

### 5.1 文档解析

`kb_pipeline_v2.py` 通过 `run_document_to_chunks()` 调用 `run.py`，复用旧链路的：

1. PDF / 文档转 Markdown；
2. 标题清洗；
3. 表格容错预处理；
4. Markdown 切 chunk。

PDF 默认走 MinerU。`.md / .txt` 等文本文件可使用 `--skip-mineru`。

### 5.2 chunk 切分

默认 chunk 大小为 `800` 字符。切分结果写入：

```text
output/<file_id>/<file_version_id>/<file_id>_chunks.json
```

每个 chunk 会尽量保留：

```text
chunk_id
id
chunk_uid
file_id
file_version_id
file_name
content
text
markdown
chapter
chapter_id
chapter_title
chunk_name
section_path
source_page
source_type
file_format
chunk_type
```

`chunk_uid` 推荐格式：

```text
<file_version_id>::<chunk_id>
```

MongoDB 中的 chunks 是后续文档溯源的权威文本来源。Neo4j 也保存 `Chunk` 节点，但主要作为图谱结构索引，不应把它当作唯一原文存储。

## 6. LLM 抽取阶段

入口：`llm_annotation_extractor.py`

输出：

```text
<file_id>.csv
<file_id>_raw_llm.jsonl
```

### 6.1 滑动窗口

LLM 每次处理一个 target chunk，可选携带前一个 context chunk。

只有满足以下条件才使用 context：

1. 前后 chunk 属于同一文件；
2. 前后 chunk 属于同一文件版本；
3. chunk_id 连续；
4. 二者都是普通非结构化文档；
5. 二者都不是表格或结构化 chunk；
6. 二者属于同一章节或同一 section。

工单、维修记录、特殊结构化数据一般不启用滑动窗口，避免跨记录误抽关系。

### 6.2 抽取格式

LLM 被要求只输出三个段落：

```text
[ENTITY]
mention | type | normalized_name | evidence

[RELATION]
source | relation_type | target | cross_chunk | involved_chunk_ids | evidence | polarity | certainty

[LOGIC_GROUP]
logic_type | members | result | involved_chunk_ids | evidence
```

实体类型只有五类：

| 中文 | 英文枚举 |
|---|---|
| 故障事件 | `FaultEvent` |
| 故障类别 | `FaultCategory` |
| 报警码 | `AlarmCode` |
| 维修方法 | `MaintenanceAction` |
| 触发规则 | `TriggerRule` |

关系类型只有七类：

| 中文 | 英文关系 |
|---|---|
| 故障触发 | `CAUSES` |
| 故障表征 | `INDICATES` |
| 故障分类 | `BELONGS_TO_CATEGORY` |
| 故障处理 | `HANDLED_BY` |
| 规则触发 | `TRIGGERED_BY_RULE` |
| 参与组合 | `PARTICIPATES_IN` |
| 组合导致 | `COMBINATION_CAUSES` |

`LOGIC_GROUP` 目前只允许 `AND`。如果原文表达 OR，应拆成多条普通因果关系。

### 6.3 evidence 设计

实体本身不记录 `start/end`。证据统一放入 evidence 数组。

证据片段格式：

```text
chunk_id::text_field::start::end::text
```

含义：

- `mention` 表达实体语义，可由 LLM 概括；
- `evidence.text` 必须来自原文；
- `start/end` 对应 evidence.text 在 `context_text` 或 `text` 中的位置；
- 同一实体多次出现时，只保留一个实体，多个位置放入多个 evidence。

## 7. 文件内实体消歧聚类

入口：

```text
entity_clustering/cluster_entities.py
```

输入：

```text
<file_id>.csv
```

输出目录：

```text
output/<file_id>/<file_version_id>/entity_clustering/
```

核心产物：

```text
cluster_intermediate.json
entity_embeddings.jsonl
cluster_candidate_diagnostics.csv
cluster_candidate_diagnostics.jsonl
```

### 7.1 为什么需要聚类

LLM 抽取的是 mention，即一次文本中出现的实体提及。同一个真实实体可能出现为：

```text
绝缘阻抗低
绝缘阻抗变低
绝缘阻抗偏低
```

如果不聚类，知识图谱会产生大量重复节点，GNR 构建故障树时会出现重复事件、断裂关系和冗余分支。

文件内聚类目标：

```text
Mention -> EntityCluster
```

并把 mention 级关系聚合为 EntityCluster 级关系。

### 7.2 Mention 数据结构

`Mention` 表示模型抽取出的原始实体提及，主要字段：

```text
mention_id
local_entity_id
file_id
sample_id
chapter_id
source_type
entity_type
mention
normalized_name
evidence
chunk_ids
neighbor_tokens
embedding
```

说明：

- `mention`：LLM 输出的实体表达；
- `normalized_name`：LLM 输出或程序规范化后的名称；
- `entity_type`：五类实体之一；
- `evidence`：证据数组；
- `chunk_ids`：出现过的 chunk；
- `neighbor_tokens`：关系邻居和上下文邻居；
- `embedding`：mention 语义向量。

### 7.3 EntityCluster 数据结构

`EntityCluster` 表示聚类后的标准实体簇，主要字段：

```text
cluster_id
file_id
entity_type
canonical_name
aliases
mention_ids
evidence
neighbor_tokens
merge_reasons
embedding
```

`canonical_name` 是簇代表名，不一定取第一个 mention，而是通过质量评分选择。

### 7.4 mention-level graph 思想

当前没有单独创建一个 mention-level graph 数据库，但在内存中为每个 mention 构造图邻居特征，并在 Neo4j 中保留 Mention 节点。

邻居分两类：

1. 关系邻居：来自 `[RELATION]` 的 source / target 端点；
2. 局部上下文邻居：同一 chunk 或相邻 chunk 中共同出现的实体。

这些邻居进入 `neighbor_tokens`，用于后续 soft neighbor similarity。

示例：

```text
out:故障处理:故障事件:通信故障
in:规则触发:触发规则:温度超过80℃持续3秒
co:FaultEvent:系统关机
```

Neo4j 中不额外写 `CO_OCCURS_WITH` 边。需要共现信息时，可以通过共同 `EVIDENCED_IN` 的 chunk 查询。

### 7.5 embedding

embedding 文本由 `mention_embedding_text()` 构造，通常包含：

```text
实体类型
mention
normalized_name
证据摘要
邻居摘要
```

支持后端：

```text
none
hash
sentence-transformers
openai-compatible
```

正式流程使用 `openai-compatible`。向量写入：

```text
entity_clustering/entity_embeddings.jsonl
```

`cluster_intermediate.json` 不保存大向量正文，只保存 `embedding_dim`，避免产物过大。

### 7.6 候选召回

聚类不会暴力比较所有实体对，而是先按：

```text
file_id + entity_type
```

分桶，只在同文件、同实体类型内比较。

候选召回函数：

```text
rank_candidate_clusters()
```

候选进入条件：

```text
name_score >= 0.45
或 embedding_score >= 0.88
或 neighbor_score >= 0.35
```

每个 mention 最多保留 TopK 个候选 cluster，默认：

```text
TopK = 20
```

这样可以避免 refinement 阶段退化成全量两两比较。

### 7.7 综合打分

核心函数：

```text
cluster_score()
```

基础公式：

```text
score =
  0.55 * name_score
  + 0.20 * embedding_score
  + 0.20 * neighbor_score
  + 0.05 * chunk_score
```

各分数含义：

- `name_score`：名称相似度，基于规范化文本和 `SequenceMatcher`；
- `embedding_score`：mention 与 cluster 向量余弦相似度；
- `neighbor_score`：soft neighbor similarity；
- `chunk_score`：同 chunk 或相邻 chunk 的弱加分。

如果 mention 或 cluster 没有 embedding，会按可用分量重新归一化权重，避免空向量把总分异常拉低。

### 7.8 soft neighbor similarity

`neighbor_score` 不是简单计算“共同邻居”。因为两个 mention 如果还没有被正确合并，它们的邻居也可能只是语义相似而不是完全同名。

当前做法是比较邻居 token 的：

```text
方向
关系类型
实体类型
邻居名称相似度
```

只要方向、关系类型、实体类型兼容，并且邻居名称相似，就可以贡献分数。

这用于处理：

```text
压力异常
```

在不同 chunk 中分别靠近：

```text
润滑油泵 / 油压低报警 / 轴承温度升高
```

或：

```text
燃气调节阀 / 燃烧室 / 燃气管路
```

时，通过邻居语义判断它更可能属于哪个实体。

### 7.9 硬冲突

在打分前会先检查 `hard_conflict()`。如果发现硬冲突，直接禁止合并。

主要规则：

1. 报警码严格匹配：`3027` 与 `3027-4` 不合并；
2. 触发规则阈值或持续时间不同，不合并；
3. 高/低、开/关、正常/异常等状态相反，不合并；
4. 交流/直流、输入/输出、正极/负极等工业限定冲突，不合并；
5. 北向/南向、上行/下行、入站/出站等方向冲突，不合并；
6. 编号冲突：如 `温湿度传感器-1` 与 `温湿度传感器-2` 不合并；
7. 未解释特异对象冲突：如 `PCS与SmartLogger` 与 `CMU与SmartLogger` 不合并；
8. 维修对象槽冲突：如 `环境温度传感器`、`冷凝器温度传感器`、`蒸发器温度传感器` 不合并；
9. 差异片段承载关键语义时，不因为公共部分很长而合并。

注意：不应靠堆大量设备专有关键词解决误合并。当前策略尽量从“特异信息是否被另一侧解释”角度做泛化判断。

### 7.10 第一轮聚类

函数：

```text
cluster_mentions()
```

步骤：

1. 按 `file_id + entity_type` 分桶；
2. 对每个 mention，先检查是否与已有 cluster 名称或 alias 强匹配；
3. 通过 `rank_candidate_clusters()` 召回 TopK 候选；
4. 对候选调用 `cluster_score()`；
5. 分数超过 `auto_threshold` 则合并；
6. 分数较高但不够自动合并的写入诊断文件；
7. 没有可信候选则创建新 cluster。

### 7.11 refinement 二次聚类

第一轮聚类可能因为候选顺序、邻居尚未稳定等原因漏合并。因此之后运行：

```text
refine_clusters()
```

默认模式：

```text
--refinement-merge-mode single
```

`single` 模式每轮只合并最可信的一个候选，比批量合并更保守。当前推荐继续使用 `single + TopK`。

refinement 会再次执行：

1. TopK 候选召回；
2. `cluster_score()`；
3. `refine_candidate_merge_reason()`；
4. 合并或保留独立簇。

### 7.12 代表名 canonical_name 选择

函数：

```text
choose_canonical_name()
canonical_name_quality()
```

聚类后会为每个实体簇选择代表名。不是简单取最长或第一个，而是综合：

- 长度是否适中；
- 动作是否完整；
- 对象是否完整；
- 触发规则是否包含对象、比较条件、阈值；
- 维修方法是否包含动作和作用对象；
- 在簇内与其他 mention 的相似中心性。

示例：

```text
检查簇控制器电池侧功率线及通讯线连接
检查功率线及通讯线连接
```

二者可以合并，但代表名倾向选择前者，因为对象信息更完整。

又如：

```text
系统压力低于运行压力P.HYD.02
低于P.HYD.02
```

二者可以合并，但代表名应倾向前者，因为它包含作用对象、比较条件和阈值编号。

## 8. 文件内聚类中间产物

`cluster_intermediate.json` 是文件内聚类后的核心产物，后续跨文件聚类和图谱导入都依赖它。

主要字段：

```text
file_id
mentions
clusters
raw_relations
clustered_relations
mention_to_cluster
diagnostic_candidates
chunks
metadata
```

说明：

- `mentions`：原始 mention；
- `clusters`：文件内聚类后的 EntityCluster；
- `raw_relations`：mention 级原始关系；
- `clustered_relations`：聚合后的 cluster 级关系；
- `mention_to_cluster`：mention 到 cluster 的映射；
- `chunks`：当前文件版本的完整 chunks，而不只是 evidence 引用到的 chunks。

`chunks` 字段很重要。Neo4j 需要完整保存该文件所有 `Chunk` 节点，不能只保存有实体证据的 chunk。

## 9. 跨文件实体聚类

入口：

```text
cross_file_entity_clustering.py
```

它处理“新上传文件完成文件内聚类后，与当前 Neo4j 中已有实体做保守合并”的问题。

### 9.1 基本原则

跨文件聚类比文件内聚类更保守。

原因：

- 不同文件可能属于不同设备、版本、型号；
- 相同名称在不同文档中可能上下文不同；
- 误合并会污染全局知识图谱。

因此跨文件只合并高置信候选，否则创建新 EntityCluster。

### 9.2 流程

```text
读取当前文件 cluster_intermediate.json
-> 从 Neo4j 读取已有 EntityCluster
-> 对每个新 cluster 构造 pseudo Mention
-> 召回同类型候选
-> 计算跨文件 score
-> score >= auto_threshold 则映射到已有 cluster
-> 否则保留为新 cluster
-> 写入 Neo4j Mention / EntityCluster / 关系 / Chunk
-> 写入 MongoDB top_event_catalog
```

### 9.3 跨文件打分

函数：

```text
score_cross_file_candidate()
```

当前公式：

```text
score =
  0.45 * name_score
  + 0.35 * embedding_score
  + 0.15 * neighbor_score
  + 0.05 * type_specific_score
```

不过当前跨文件候选从 Neo4j 读取时主要拿到的是结构属性和 `embedding_dim`，并不直接从 Neo4j 读取完整向量。因此跨文件 embedding 能力还不是最终形态。后续如果要增强跨文件合并，需要设计全局实体向量存储或可回查的 embedding 文件。

### 9.4 映射策略

跨文件聚类不会直接修改本地 `cluster_intermediate.json` 的 cluster_id，而是生成：

```text
cluster_mapping: local_cluster_id -> mapped_cluster_id
```

导入 Neo4j 时：

- 如果映射到已有 cluster，则把新文件的 mention、证据、chunk、source 信息并入已有 EntityCluster；
- 如果没有匹配，则创建新的 EntityCluster。

## 10. Neo4j 存储结构

当前 Neo4j 内部使用英文稳定枚举，中文只作为展示属性保存。

### 10.1 节点

主要节点：

```text
(:File)
(:Chunk)
(:Mention:<EntityType>)
(:EntityCluster:<EntityType>)
```

实体类型标签为：

```text
FaultEvent
FaultCategory
AlarmCode
MaintenanceAction
TriggerRule
```

### 10.2 File

```text
file_id
file_version_id
file_name
source_file_ids
source_file_version_ids
updated_at
```

### 10.3 Chunk

```text
chunk_uid
file_id
file_version_id
chunk_id
content
text
markdown
chapter
chapter_id
chapter_title
chunk_name
section_path
source_page
source_file_ids
source_file_version_ids
updated_at
```

Neo4j 会保存完整 Chunk 节点，且不会让 evidence 引用到的 chunk 重复导入。

### 10.4 Mention

```text
mention_id
file_id
file_version_id
sample_id
chapter_id
source_type
entity_type
entity_type_code
entity_type_zh
mention
normalized_name
chunk_ids
source_file_ids
source_file_version_ids
neighbor_tokens
evidence_json
cluster_id
embedding_dim
updated_at
```

Mention 表示原始抽取结果，用于证据溯源、聚类调试和下钻查看。

### 10.5 EntityCluster

```text
cluster_id
entity_type
entity_type_code
entity_type_zh
canonical_name
name
aliases
mention_ids
mention_count
chunk_ids
source_file_ids
source_file_version_ids
neighbor_tokens
merge_reasons
evidence_json
embedding_dim
updated_at
```

注意：

- `EntityCluster` 不再依赖单值 `file_id / file_version_id`；
- 跨文件合并后，`source_file_ids` 和 `source_file_version_ids` 可以包含多个来源；
- GNR 查询文件视图时，应按 `source_file_version_ids` 过滤。

### 10.6 结构关系

```text
(:File)-[:HAS_CHUNK]->(:Chunk)
(:Mention)-[:EVIDENCED_IN]->(:Chunk)
(:Mention)-[:RESOLVED_TO]->(:EntityCluster)
```

### 10.7 语义关系

Mention 层关系：

```text
MENTION_CAUSES
MENTION_INDICATES
MENTION_BELONGS_TO_CATEGORY
MENTION_HANDLED_BY
MENTION_TRIGGERED_BY_RULE
MENTION_PARTICIPATES_IN
MENTION_COMBINATION_CAUSES
```

EntityCluster 层关系：

```text
CLUSTERED_CAUSES
CLUSTERED_INDICATES
CLUSTERED_BELONGS_TO_CATEGORY
CLUSTERED_HANDLED_BY
CLUSTERED_TRIGGERED_BY_RULE
CLUSTERED_PARTICIPATES_IN
CLUSTERED_COMBINATION_CAUSES
```

关系属性：

```text
relation_id
file_id
file_version_id
source_file_ids
source_file_version_ids
relation_type
relation_type_code
relation_type_zh
relation_level
cross_chunk
involved_chunk_ids
evidence_json
source_relation_ids
polarity
certainty
updated_at
```

关系仍保留单值 `file_id / file_version_id` 作为本次导入来源，同时也保留数组型 `source_file_ids / source_file_version_ids` 供跨文件视图过滤。

## 11. MongoDB 存储结构

主要集合：

```text
files
file_versions
chunks
top_event_catalog
kb_jobs / pipeline jobs
```

### 11.1 chunks

MongoDB 的 `chunks` 是文档溯源时的权威来源。GNR 前端点击节点或关系详情时，应通过 chunk id / file_version_id 回查 MongoDB，展示 chunk 原文。

### 11.2 top_event_catalog

`top_event_catalog` 由 `EntityCluster:FaultEvent` 生成，是 GNR 选择顶事件、顶事件语义匹配和批量发现顶事件的入口。

核心字段：

```text
_id
graph_node_id
file_id
file_version_id
file_name
source_file_ids
source_file_version_ids
source_file_scopes
is_active
name
display_name
normalized_name
aliases
normalized_aliases
source_chunk_ids
chunk_refs
evidence_json
semantic_text
embedding
embedding_model
embedding_updated_at
created_at
updated_at
```

说明：

- `graph_node_id` 对应 Neo4j `EntityCluster.cluster_id`；
- `semantic_text` 用于生成顶事件 embedding；
- `embedding` 是顶事件语义匹配所需向量；
- 新 KB v2 流程在 `cross_file_entity_clustering.py` 的 `upsert_top_event_catalog()` 中自动写入 embedding；
- 旧库如果缺少 embedding，可运行 `repair_top_event_catalog_embeddings.py` 补齐。

修复命令：

```powershell
.\.venv\Scripts\python.exe repair_top_event_catalog_embeddings.py
```

只修某个文件版本：

```powershell
.\.venv\Scripts\python.exe repair_top_event_catalog_embeddings.py --file-version-id huawei_luna2000_alarm_reference_v1
```

## 12. 文件版本与跨文件视图

KB 支持同一文件多版本。每次上传都会有：

```text
file_id
file_version_id
version_no
```

完成 KB v2 后会调用 `activate_file_version()` 激活当前版本。

GNR 构建故障树时，前端会传入：

```text
selected_file_version_ids
```

GNR 的当前文件视图规则：

1. EntityCluster 只要 `source_file_version_ids` 与所选版本有交集，就可见；
2. 关系只有其 `source_file_version_ids` 与所选版本有交集，才可见；
3. 文档溯源只展示所选文件版本支持的 chunks；
4. 跨文件聚类后的同一 EntityCluster 可以同时承载多个文件来源；
5. 如果用户选择 A、B 两个文件，且二者都支持同一实体 C，则图中只出现一个 C，但 C 的关系会按所选文件范围过滤。

KB 不在写库阶段删除跨文件形成的环。KB 只应清理自环；普通因果环和冗余路径由 GNR 在当前 selected file view 内临时处理。

## 13. 与 GNR 的接口边界

KB 的职责：

```text
构建 chunks
抽取实体关系
实体消歧聚类
写入 MongoDB / Neo4j
生成 top_event_catalog
```

GNR 的职责：

```text
根据 selected_file_version_ids 限定视图
匹配顶事件
召回 EntityCluster 子图
召回 MongoDB chunks
临时处理当前视图中的环和冗余路径
生成故障树 JSON
校验和修复故障树
```

GNR 不应在生成故障树时触发 KB 抽取或实体聚类；它只读取 KB 已构建好的 MongoDB / Neo4j 数据。

## 14. 常用调试命令

### 14.1 跑完整 KB v2

```powershell
.\.venv\Scripts\python.exe kb_pipeline_v2.py `
  --env-file .env `
  --input-file "E:\path\to\manual.pdf" `
  --output-dir output `
  --file-id file_demo `
  --file-version-id file_demo_v1 `
  --file-name "示例设备手册.pdf" `
  --source-type manual_document `
  --file-format pdf `
  --chunk-size 800 `
  --workers 1 `
  --embedding-backend openai-compatible
```

### 14.2 只跑文件内聚类

```powershell
.\.venv\Scripts\python.exe entity_clustering\cluster_entities.py `
  --env-file entity_clustering\.env `
  --input-csv output\file_demo\file_demo_v1\file_demo.csv `
  --file-id file_demo `
  --output-dir output\file_demo\file_demo_v1\entity_clustering `
  --embedding-backend openai-compatible `
  --refinement-merge-mode single `
  --candidate-top-k 20 `
  --candidate-min-name 0.45 `
  --candidate-min-embedding 0.88 `
  --candidate-min-neighbor 0.35
```

### 14.3 只跑跨文件导入

```powershell
.\.venv\Scripts\python.exe cross_file_entity_clustering.py `
  --env-file .env `
  --intermediate-json output\file_demo\file_demo_v1\entity_clustering\cluster_intermediate.json `
  --file-id file_demo `
  --file-version-id file_demo_v1 `
  --file-name "示例设备手册.pdf" `
  --diagnostics-jsonl output\file_demo\file_demo_v1\cross_file_diagnostics.jsonl
```

### 14.4 补齐顶事件 embedding

```powershell
.\.venv\Scripts\python.exe repair_top_event_catalog_embeddings.py
```

### 14.5 清理 Neo4j 自环

```powershell
.\.venv\Scripts\python.exe cleanup_neo4j_self_loops.py --env-file .env --apply
```

## 15. 当前实现需要注意的问题

1. 跨文件 embedding 仍有提升空间。当前 Neo4j 不保存完整实体向量，只保存 `embedding_dim`；跨文件真正基于向量的全局合并还需要更稳定的向量存储策略。
2. `cluster_candidate_diagnostics.*` 是调试产物，不是人工审核主流程。正式导入不会要求人工审核候选。
3. `EntityCluster` 的 `source_file_ids/source_file_version_ids` 是 GNR 文件视图过滤的关键字段，不要移除。
4. `Mention` 层必须保留。它是证据溯源、聚类诊断和后续错误追查的基础。
5. MongoDB chunks 是原文权威来源；故障树 JSON 不应长期保存完整 chunk 原文。
6. KB 写库阶段不要为了让故障树无环而删除普通因果边；GNR 会在用户选择文件后的当前视图里临时去环。
7. LLM 抽取结果中的 evidence start/end 可能有偏移，因此标注数据阶段有独立的 start/end 规范化脚本；正式 KB 仍应尽量保留原始 evidence 以便追溯。

## 16. 一句话总结

当前 KB v2 的核心思想是：**用 LLM 从 chunk 滑动窗口中抽取 mention 级故障知识；用文件内聚类和跨文件保守聚类把 mention 合并成稳定 EntityCluster；在 Neo4j 中同时保留 Mention 层和 EntityCluster 层，在 MongoDB 中保存 chunks 与 top_event_catalog；GNR 只在用户选择的文件版本视图下读取这些知识来生成故障树。**

