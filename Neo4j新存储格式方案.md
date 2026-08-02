# Neo4j 新存储格式方案

本文档用于 GNR 与前端 AI 助手开发阶段对接新的知识图谱结构。核心原则是：**同一张图中同时保留 Mention 层与 EntityCluster 层，但业务推理优先使用 EntityCluster；只有证据溯源、人工检查、聚类调试时才下钻到 Mention。**

## 节点标签

不再使用 `SmartFTAFile`、`SmartFTAChunk`、`SmartFTAMention`、`SmartFTAEntityCluster` 这类前缀标签，只保留清晰的基础标签：

- `File`
- `Chunk`
- `Mention`
- `EntityCluster`

Mention 和 EntityCluster 会额外带一个英文实体类型标签，五类之一：

- `FaultEvent`
- `FaultCategory`
- `AlarmCode`
- `MaintenanceAction`
- `TriggerRule`

示例：

```cypher
(:Mention:FaultEvent {mention_id, mention, normalized_name, entity_type})
(:EntityCluster:FaultEvent {cluster_id, canonical_name, entity_type})
```

## 节点结构

### File

```cypher
(:File {
  file_id,
  file_version_id,
  file_name
})
```

用于标识一次被导入的文档版本。

### Chunk

```cypher
(:Chunk {
  chunk_uid,
  file_id,
  file_version_id,
  chunk_id,
  chapter_id
})
```

`chunk_uid` 推荐格式为：`<file_version_id>::<chunk_id>`。

### Mention

模型在某个样本中抽取到的原始实体提及：

```cypher
(:Mention:FaultEvent {
  mention_id,
  file_id,
  file_version_id,
  sample_id,
  chapter_id,
  entity_type: "FaultEvent",
  entity_type_code: "FaultEvent",
  entity_type_zh: "故障事件",
  mention,
  normalized_name,
  chunk_ids,
  evidence_json,
  cluster_id
})
```

Mention 不直接作为 GNR 的主要推理节点，它用于保留原始抽取结果、原始关系和证据。

### EntityCluster

实体消歧聚类后的标准实体：

```cypher
(:EntityCluster:FaultEvent {
  cluster_id,
  file_id,
  file_version_id,
  entity_type: "FaultEvent",
  entity_type_code: "FaultEvent",
  entity_type_zh: "故障事件",
  canonical_name,
  aliases,
  mention_ids,
  mention_count,
  chunk_ids,
  evidence_json
})
```

GNR 构建故障树、检索候选顶事件、组织故障证据时，应优先查询 EntityCluster。

## 结构关系

保留两类结构关系：

```cypher
(:File)-[:HAS_CHUNK]->(:Chunk)
(:Mention)-[:EVIDENCED_IN]->(:Chunk)
(:Mention)-[:RESOLVED_TO]->(:EntityCluster)
```

`EVIDENCED_IN` 表示 Mention 来自哪些 chunk。

`RESOLVED_TO` 表示 Mention 被聚类/消歧到哪个标准实体。

## 语义关系

不再使用统一的 `MENTION_RELATION` / `CLUSTERED_RELATION`，而是拆成 typed edge。为了仍然区分 Mention 层与 Cluster 层，关系类型使用前缀：

- Mention 层：`MENTION_关系类型`
- EntityCluster 层：`CLUSTERED_关系类型`

支持的 Mention 层关系：

- `MENTION_CAUSES`
- `MENTION_INDICATES`
- `MENTION_BELONGS_TO_CATEGORY`
- `MENTION_HANDLED_BY`
- `MENTION_TRIGGERED_BY_RULE`
- `MENTION_PARTICIPATES_IN`
- `MENTION_COMBINATION_CAUSES`

支持的 EntityCluster 层关系：

- `CLUSTERED_CAUSES`
- `CLUSTERED_INDICATES`
- `CLUSTERED_BELONGS_TO_CATEGORY`
- `CLUSTERED_HANDLED_BY`
- `CLUSTERED_TRIGGERED_BY_RULE`
- `CLUSTERED_PARTICIPATES_IN`
- `CLUSTERED_COMBINATION_CAUSES`

关系属性保留原始中文关系类型和层级：

```cypher
(:EntityCluster)-[:CLUSTERED_CAUSES {
  relation_type: "CAUSES",
  relation_type_code: "CAUSES",
  relation_type_zh: "故障触发",
  relation_level: "clustered",
  polarity,
  certainty,
  cross_chunk,
  involved_chunk_ids,
  evidence_json,
  source_relation_ids
}]->(:EntityCluster)
```

## GNR 推荐查询

### 查询聚类后的故障触发关系

```cypher
MATCH (cause:EntityCluster)-[r:CLUSTERED_CAUSES]->(effect:EntityCluster)
RETURN cause, r, effect
```

### 查询某个实体簇的聚类后邻居

```cypher
MATCH (e:EntityCluster {cluster_id: $cluster_id})-[r]-(n:EntityCluster)
WHERE r.relation_level = "clustered"
RETURN e, r, n
```

### 查询某条聚类关系背后的 Mention 证据

```cypher
MATCH (a:EntityCluster {cluster_id: $source_cluster_id})<-[:RESOLVED_TO]-(ma:Mention)
MATCH (b:EntityCluster {cluster_id: $target_cluster_id})<-[:RESOLVED_TO]-(mb:Mention)
MATCH (ma)-[r]->(mb)
WHERE r.relation_level = "mention"
RETURN ma, r, mb
```

### 从 EntityCluster 下钻到原文 chunk

```cypher
MATCH (e:EntityCluster {cluster_id: $cluster_id})<-[:RESOLVED_TO]-(m:Mention)-[:EVIDENCED_IN]->(c:Chunk)
RETURN e, m, c
```

## MongoDB 对接

MongoDB 中仍保留：

- `files`
- `file_versions`
- `chunks`
- `top_event_catalog`

其中 `top_event_catalog` 由 `EntityCluster:FaultEvent` 类型生成，字段包括：

- `file_id`
- `file_version_id`
- `name`
- `normalized_name`
- `aliases`
- `source_chunk_ids`
- `graph_node_id`

GNR 选择顶事件时可以继续读取 `top_event_catalog`，再用 `graph_node_id` 回到 Neo4j 中定位对应的 `EntityCluster`。
