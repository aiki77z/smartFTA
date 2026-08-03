# GNR 对接 KB V2 接口检查说明

本文档记录 GNR 在对接新版 KB 图谱后的接口注意点。

## 1. 前端与 GNR 的文件作用域

故障树生成接口仍然应传入：

```text
selected_file_version_ids
```

GNR 必须以这些 `file_version_id` 作为当前知识库视图范围。新版 KB 支持跨文件实体聚类，同一个 `EntityCluster` 可能由多个文件版本共同支持，因此不能只看节点单值 `file_version_id`，需要看：

```text
source_file_version_ids
```

只有实体或关系的 `source_file_version_ids` 与用户选择的文件版本有交集时，才允许进入当前故障树构建上下文。

## 2. Neo4j 新图谱主查询对象

GNR 业务推理应优先查询聚类后的实体层：

```cypher
(:EntityCluster:FaultEvent)
```

因果边使用：

```cypher
(:EntityCluster)-[:CLUSTERED_CAUSES]->(:EntityCluster)
```

不要再依赖旧结构：

```cypher
(:Entity:FaultPhenomenon)-[:RELATION]->(:Entity)
```

Mention 层只用于证据、溯源和聚类调试，不应作为故障树主推理节点。

## 3. graph_node_id 的含义

新版 KB 的 `top_event_catalog.graph_node_id` 保存的是稳定的：

```text
EntityCluster.cluster_id
```

GNR 已兼容：

- `graph_node_id` 是 Neo4j `elementId`
- `graph_node_id` 是 `EntityCluster.cluster_id`

后续多智能体接口建议优先传递和保存 `cluster_id`，不要依赖 Neo4j 临时 `elementId`。

## 4. 已适配的 GNR 接口点

已适配 `FTA-GNR/database.py` 中的核心图谱读取函数：

- `match_top_event_from_graph`
- `get_graph_node_by_id`
- `expand_local_fault_subgraph`
- `expand_scoped_local_fault_subgraph`
- `list_graph_top_event_candidates`
- `search_chunks_by_entity_names`
- `update_graph_node_properties`

这些函数现在会按 `source_file_version_ids` 做作用域过滤，并兼容 `canonical_name/name/normalized_name/aliases`。

## 5. 多智能体部署阶段建议

多智能体拆分时，建议保持以下边界：

- TopEventAgent：只通过 `top_event_catalog` 和 `match_top_event_from_graph` 解析顶事件。
- GraphRetrievalAgent：只通过 `expand_scoped_local_fault_subgraph` 召回子图。
- EvidenceAgent：通过 `collect_subgraph_chunks` 与 `get_chunks_by_ids` 组织证据 chunks。
- DraftAgent：基于子图、逻辑组、证据 chunks 生成故障树草案。

不要让各智能体直接拼写 Neo4j schema 查询，避免后续 KB schema 调整时多处失配。
