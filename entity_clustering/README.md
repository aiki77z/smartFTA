# Entity Clustering / 实体消歧聚类模块

本目录是 SmartFTA KB 模块里的实体消歧聚类原型。它接收人工审核后的标注 CSV，将抽取模型输出的实体 mention 聚类为标准实体簇，并可将 mention-level graph 与 entity-cluster graph 一起导入 Neo4j。

当前推荐运行方式是：

```text
single refinement + TopK candidate retrieval + embedding
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `.env.example` | 环境变量模板，包含 Neo4j 配置、embedding API 配置、默认输入输出路径示例 |
| `cluster_entities.py` | 核心聚类脚本：读取审核 CSV、解析实体关系、生成 mention、构造邻居、embedding、聚类、导出结果 |
| `import_cluster_graph_to_neo4j.py` | 在 `cluster_entities.py` 基础上，将 chunk、mention、entity cluster 和聚合关系导入 Neo4j |
| `debug_cluster_scores.py` | 调试脚本：查看已合并样例、未合并高分候选、诊断候选及各项 score |
| `evaluate_clustering.py` | 弱评估脚本：用 `file_id + entity_type + normalized_name` 构造弱 gold，检查碎片化和误合并 |
| `run_clustering_smoke_tests.py` | 快速 smoke test，验证关键冲突和合并规则没有被破坏 |
| `实体消歧聚类当前实现说明.md` | 当前算法设计、图结构、测试结果和优化记录的详细说明文档 |
| `output/` | 本地输出目录，保存 embedding 缓存、聚类 CSV、诊断文件等。已被 `.gitignore` 忽略，不提交 |

## 配置

复制模板：

```powershell
Copy-Item entity_clustering\.env.example entity_clustering\.env
```

在 `entity_clustering/.env` 中配置：

```env
NEO4J_URI=neo4j://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_password_here
NEO4J_DATABASE=neo4j

ENTITY_CLUSTER_EMBEDDING_BACKEND=openai-compatible
ENTITY_CLUSTER_EMBEDDING_API_KEY=your-dashscope-api-key-here
ENTITY_CLUSTER_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ENTITY_CLUSTER_EMBEDDING_MODEL=text-embedding-v4
ENTITY_CLUSTER_EMBEDDING_BATCH_SIZE=10
```

也可以使用与 `FTA-GNR` 兼容的 fallback 名称：

```env
EMBEDDING_API_KEY=your-dashscope-api-key-here
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_BATCH_SIZE=10
```

## 输入数据要求

输入通常是 `annotation-data-pipeline` 中人工审核后的 CSV，需要至少包含：

```text
sample_id
file_id
text
entities_json
relations_json
logic_groups_json
```

兼容旧字段：

```text
context_entities_json
target_entities_json
```

实体 evidence 推荐格式：

```json
[
  {
    "chunk_id": "23",
    "text_field": "text",
    "start": 1,
    "end": 6,
    "text": "主油泵故障"
  }
]
```

## 只跑聚类并导出文件

在 `FTA-KB` 目录下运行。

### 使用 DashScope / OpenAI-compatible embedding

```powershell
.\.venv\Scripts\python.exe entity_clustering\cluster_entities.py `
  --env-file entity_clustering\.env `
  --input-csv "..\annotation-data-pipeline\wyf\reviewed_annotations_dg50002266_v1_offsets_fixed(1).csv" `
  --file-id file_dg50002266_00_hydraulic_oil_module_manual `
  --output-dir entity_clustering\output\dg50002266_dashscope `
  --embedding-backend openai-compatible `
  --refinement-max-rounds 8 `
  --candidate-top-k 20 `
  --candidate-min-name 0.45 `
  --candidate-min-embedding 0.88 `
  --candidate-min-neighbor 0.35
```

### 复用已有 embedding

如果已经生成过 `entity_embeddings.jsonl`，建议复用，避免重复调用 API：

```powershell
.\.venv\Scripts\python.exe entity_clustering\cluster_entities.py `
  --env-file entity_clustering\.env `
  --input-csv "..\annotation-data-pipeline\wyf\reviewed_annotations_dg50002266_v1_offsets_fixed(1).csv" `
  --file-id file_dg50002266_00_hydraulic_oil_module_manual `
  --output-dir entity_clustering\output\dg50002266_dashscope `
  --embedding-backend openai-compatible `
  --reuse-embeddings-jsonl entity_clustering\output\dg50002266_dashscope\entity_embeddings.jsonl `
  --refinement-max-rounds 8 `
  --candidate-top-k 20
```

### 轻量本地测试，不调用 embedding API

```powershell
.\.venv\Scripts\python.exe entity_clustering\cluster_entities.py `
  --input-csv "..\annotation-data-pipeline\wyf\reviewed_annotations_dg50002266_v1_offsets_fixed(1).csv" `
  --file-id file_dg50002266_00_hydraulic_oil_module_manual `
  --output-dir entity_clustering\output\dg50002266_hash `
  --embedding-backend hash `
  --refinement-max-rounds 8
```

## 聚类输出

`cluster_entities.py` 会在 `--output-dir` 下生成：

| 文件 | 内容 |
|---|---|
| `entity_mentions.csv` | 所有 mention 实例，包含原始名称、类型、证据、邻居 token |
| `entity_clusters.csv` | 聚类后的标准实体簇，包含 canonical name、aliases、mention_ids、merge_reasons |
| `clustered_relations.csv` | mention 级关系映射到 entity cluster 后的聚合关系 |
| `entity_embeddings.jsonl` | mention embedding 缓存，仅在启用 embedding 时生成 |
| `cluster_summary.json` | 聚类统计摘要 |
| `diagnostics/cluster_candidate_diagnostics.csv` | 边界候选诊断信息 |
| `diagnostics/cluster_candidate_diagnostics.jsonl` | 边界候选诊断信息 JSONL |

## 导入 Neo4j

推荐直接用 `import_cluster_graph_to_neo4j.py` 跑完整流程并导入 Neo4j。

### LUNA2000 示例

```powershell
.\.venv\Scripts\python.exe entity_clustering\import_cluster_graph_to_neo4j.py `
  --env-file entity_clustering\.env `
  --input-csv "..\annotation-data-pipeline\zyt\HUAWEI LUNA2000-(97KWH-200KWH)系列 工商业构网型储能系统 告警参考_organized.csv" `
  --file-id huawei_luna2000_alarm_reference `
  --output-dir entity_clustering\output\luna2000_dashscope `
  --embedding-backend openai-compatible `
  --reuse-embeddings-jsonl entity_clustering\output\luna2000_dashscope\entity_embeddings.jsonl `
  --refinement-max-rounds 8 `
  --candidate-top-k 20 `
  --candidate-min-name 0.45 `
  --candidate-min-embedding 0.88 `
  --candidate-min-neighbor 0.35
```

### DG50002266 示例

```powershell
.\.venv\Scripts\python.exe entity_clustering\import_cluster_graph_to_neo4j.py `
  --env-file entity_clustering\.env `
  --input-csv "..\annotation-data-pipeline\wyf\reviewed_annotations_dg50002266_v1_offsets_fixed(1).csv" `
  --file-id file_dg50002266_00_hydraulic_oil_module_manual `
  --output-dir entity_clustering\output\dg50002266_dashscope `
  --embedding-backend openai-compatible `
  --reuse-embeddings-jsonl entity_clustering\output\dg50002266_dashscope\entity_embeddings.jsonl `
  --refinement-max-rounds 8 `
  --candidate-top-k 20
```

默认会先清空当前 `file_id` 对应的旧图谱，再导入新图谱。若不想清空，添加：

```powershell
--no-clear
```

## Neo4j 图结构

导入后的主要节点：

| 节点 | 含义 |
|---|---|
| `SmartFTAChunk` | chunk 节点 |
| `SmartFTAMention` | 原始实体 mention |
| `SmartFTAEntityCluster` | 聚类后的标准实体簇 |

主要关系：

| 关系 | 含义 |
|---|---|
| `(SmartFTAMention)-[:EVIDENCED_IN]->(SmartFTAChunk)` | mention 的证据来源 |
| `(SmartFTAMention)-[:RESOLVED_TO]->(SmartFTAEntityCluster)` | mention 解析到实体簇 |
| `(SmartFTAMention)-[:MENTION_RELATION]->(SmartFTAMention)` | 原始 mention 级关系 |
| `(SmartFTAEntityCluster)-[:CLUSTERED_RELATION]->(SmartFTAEntityCluster)` | 聚合后的实体簇级关系 |

不创建 `File` 节点，也不创建单独的 relationship 节点。

## 常用 Cypher

查看某个文件的节点数：

```cypher
MATCH (n)
WHERE n.file_id = "huawei_luna2000_alarm_reference"
RETURN labels(n)[0] AS label, count(*) AS cnt
ORDER BY label;
```

查看多个 mention 指向同一个实体簇：

```cypher
MATCH (m:SmartFTAMention)-[:RESOLVED_TO]->(c:SmartFTAEntityCluster)
WHERE c.file_id = "huawei_luna2000_alarm_reference"
WITH c, collect(m) AS mentions
WHERE size(mentions) > 1
RETURN c, mentions
LIMIT 50;
```

查看不包含 mention 的实体簇级关系图：

```cypher
MATCH (a:SmartFTAEntityCluster)-[r:CLUSTERED_RELATION]->(b:SmartFTAEntityCluster)
RETURN a, r, b
LIMIT 300;
```

查看同一 chunk 中共现的 mention：

```cypher
MATCH (m:SmartFTAMention {mention_id: $mention_id})-[:EVIDENCED_IN]->(c:SmartFTAChunk)<-[:EVIDENCED_IN]-(neighbor:SmartFTAMention)
WHERE neighbor <> m
RETURN c, neighbor;
```

## Debug 分数

推荐使用 `--output-file`，不要用 PowerShell 的 `>` 重定向，否则可能生成非 UTF-8 文件。

```powershell
.\.venv\Scripts\python.exe entity_clustering\debug_cluster_scores.py `
  --output-dir entity_clustering\output\luna2000_dashscope `
  --output-file entity_clustering\output\luna2000_dashscope\debug_cluster_scores.txt `
  --merged-limit 10 `
  --unmerged-limit 60 `
  --scan-unmerged-pairs `
  --scan-limit 100 `
  --scan-min-score 0.50
```

输出内容包括：

| 段落 | 含义 |
|---|---|
| `[MERGED]` | 已合并簇中的 mention pair 及分数 |
| `[SCANNED_UNMERGED_PAIRS]` | TopK 扫描发现的未合并高分候选 |
| `[UNMERGED_DIAGNOSTIC_CANDIDATES]` | 聚类过程中记录的边界候选 |

调试输出中的关键分数：

| 分数 | 含义 |
|---|---|
| `name_score` | 名称相似度 |
| `embedding_score` | 向量相似度 |
| `neighbor_score` | 关系邻居/上下文邻居相似度 |
| `chunk_score` | 是否来自同一证据 chunk |
| `reasons` | boost、hard conflict、object slot conflict 等解释 |

## 评估脚本

`evaluate_clustering.py` 使用 `file_id + entity_type + normalized_name` 构造弱 gold。它不是最终人工 gold 评估，但可以发现明显碎片化和误合并。

```powershell
.\.venv\Scripts\python.exe entity_clustering\evaluate_clustering.py `
  --input-csv "..\annotation-data-pipeline\wyf\reviewed_annotations_dg50002266_v1_offsets_fixed(1).csv" `
  --file-id file_dg50002266_00_hydraulic_oil_module_manual `
  --output-dir entity_clustering\output\dg50002266_eval
```

输出：

| 文件 | 含义 |
|---|---|
| `cluster_eval_summary.json` | 总体弱评估统计 |
| `cluster_eval_fragmentation.csv` | 同一弱 gold 被拆成多个 cluster 的情况 |
| `cluster_eval_overmerge.csv` | 一个 cluster 包含多个弱 gold 的情况 |

## Smoke Test

每次修改聚类规则后建议运行：

```powershell
.\.venv\Scripts\python.exe entity_clustering\run_clustering_smoke_tests.py
```

它会检查：

- 高/低、输入/输出、交流/直流等冲突不会误合并；
- `簇内/簇间` 这类关键最小差异片段不会误合并；
- 名称、embedding、邻居足够一致时可以合并；
- 触发规则 canonical 会优先选择信息更完整的表达；
- 维修方法 canonical 会优先选择操作对象更完整的表达。

## 当前核心策略简述

### 候选召回

只比较同文件、同实体类型的实体。召回条件：

```text
name_score >= 0.45
或 embedding_score >= 0.88
或 neighbor_score >= 0.35
```

每个 mention 最多保留 TopK=20 个候选 cluster。

### 综合打分

```text
score =
  0.55 * name_score
  + 0.20 * embedding_score
  + 0.20 * neighbor_score
  + 0.05 * chunk_score
```

同时支持：

- `graph_boost`：关系邻居高度一致时提升合并倾向；
- `semantic_boost`：名称和 embedding 双高时提升合并倾向；
- hard conflict：报警码、触发规则签名、方向/编号/对象槽冲突直接阻断合并。

### canonical name 选择

不同实体类型使用不同偏好：

| 类型 | 策略 |
|---|---|
| 报警码 | 严格码值，通常不需要额外信息增益 |
| 触发规则 | 优先选择覆盖同一规则 signature 且信息更完整的表达，例如 `系统压力低于运行压力P.HYD.02` |
| 维修方法 | 优先选择维修动作对象更完整的表达，例如 `检查簇控制器电池侧功率线及通讯线连接` |
| 故障事件 | 暂不启用信息增益，避免把上下位/泛化故障强行合并 |
| 故障类别 | 暂不做特殊策略，短标准名通常更合适 |

## 注意事项

- `entity_clustering/.env`、`entity_clustering/output/`、embedding 缓存和调试输出都被 `.gitignore` 忽略，不要提交真实数据和 API key。
- 如果只是重新调聚类规则，优先使用 `--reuse-embeddings-jsonl`。
- `strong-batch` 是实验模式，当前默认不推荐。常规导入使用 `single + TopK`。
- PowerShell 输出中文时可能显示乱码，但源码和文档均按 UTF-8 保存；调试报告请使用 `--output-file`。

