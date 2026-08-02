# 跨文件实体聚类与 GNR 文件视图方案

当前实体消歧聚类已经完成“同一文件内”的 Mention -> EntityCluster 聚类。后续如果用户上传多个文件，可能出现不同文件中描述同一故障事件、报警码、维修方法或触发规则的情况，因此需要在文件内聚类完成后，再进行一次跨文件聚类。

本方案采用一个更适合 GNR 的设计：**不新增 GlobalEntity 层，跨文件合并后的标准实体仍然叫 EntityCluster**。

也就是说：

- `Mention` 保留每次抽取到的原始实体提及；
- `EntityCluster` 表示同一含义的标准实体，可以由一个文件支持，也可以由多个文件共同支持；
- `EntityCluster` 与 `EntityCluster` 之间的关系也可以由一个或多个文件支持；
- GNR 查询时通过用户选择的 `file_id + file_version_id` 过滤实体和关系的可见范围。

这样可以保证：如果用户同时选择文件 A 和文件 B，且两个文件都提到同一个实体 C，那么故障树中只出现一个 C，并同时保留 C 与 D、C 与 E 的关系。

## 1. 为什么不使用 GlobalEntity

之前考虑过新增：

```cypher
(:EntityCluster)-[:GLOBAL_RESOLVED_TO]->(:GlobalEntity)
```

但这会带来一个问题：GNR 构建故障树时真正需要的是一个统一实体节点，而不是“文件内实体 + 全局实体”的两层跳转。

如果 GNR 只召回 `EntityCluster`，跨文件合并能力用不上；如果召回 `GlobalEntity`，又需要额外把关系从 `EntityCluster` 聚合到 `GlobalEntity`，查询和溯源都会变复杂。

因此更推荐：

```cypher
(:Mention)-[:RESOLVED_TO]->(:EntityCluster)
(:EntityCluster)-[:CLUSTERED_CAUSES]->(:EntityCluster)
```

其中 `EntityCluster` 本身就是跨文件聚类后的标准实体。

## 2. EntityCluster 需要保存的文件范围

跨文件合并后，一个 `EntityCluster` 需要清晰记录它被哪些文件支持。

推荐属性：

```cypher
(:EntityCluster {
  cluster_id,
  entity_type,
  entity_type_zh,
  canonical_name,
  aliases,
  mention_ids,
  source_file_ids,
  source_file_version_ids,
  source_file_scopes,
  chunk_refs,
  evidence_json
})
```

其中：

- `source_file_ids`: 支持该实体的 `file_id` 列表；
- `source_file_version_ids`: 支持该实体的 `file_version_id` 列表；
- `source_file_scopes`: 推荐保存为字符串列表，例如 `fileA::fileA_v1`；
- `chunk_refs`: 推荐保存为字符串列表，例如 `fileA_v1::12`；
- `evidence_json`: 保留所有 Mention 证据，证据中也应包含 `file_id`、`file_version_id`、`chunk_id`。

说明：用户可能上传同一文件的不同版本，因此单独用 `file_id` 不够，必须使用 `file_id + file_version_id` 共同限定文件版本。

## 3. EntityCluster 之间关系需要保存的文件范围

跨文件聚合后的关系也需要记录由哪些文件支持。

示例：

```cypher
(:EntityCluster)-[:CLUSTERED_CAUSES {
  relation_type: "CAUSES",
  relation_type_zh: "故障触发",
  relation_level: "clustered",
  source_file_ids,
  source_file_version_ids,
  source_file_scopes,
  involved_chunk_refs,
  evidence_json,
  source_relation_ids
}]->(:EntityCluster)
```

关系的可见性不是由两端实体决定，而是由关系自身的 `source_file_version_ids` 决定。

例如：

- 文件 A 支持 `C -> D`
- 文件 B 支持 `C -> E`

当用户选择 A+B 时，GNR 能看到：

- 一个统一的实体 C；
- 关系 `C -> D`；
- 关系 `C -> E`。

当用户只选择 A 时，GNR 只能看到：

- 实体 C；
- 实体 D；
- 关系 `C -> D`；
- 看不到只由文件 B 支持的 `C -> E`。

## 4. 跨文件聚类流程

推荐流程：

1. 每个文件先独立完成 Mention -> EntityCluster；
2. 把新文件生成的 EntityCluster 与当前图谱中已有 EntityCluster 做候选召回；
3. 只比较少量候选，跨文件阈值高于同文件内；
4. 如果判断为同一实体，则把新文件的 Mention 通过 `RESOLVED_TO` 指向已有 EntityCluster；
5. 更新该 EntityCluster 的 `source_file_ids`、`source_file_version_ids`、`mention_ids`、`aliases`、`evidence_json`；
6. 将新文件中的聚类关系写到标准 EntityCluster 之间；
7. 如果关系两端已经是已有 EntityCluster，则合并关系属性和证据，不重复建边。

## 5. 跨文件候选召回

为了避免速度太慢，不做全量两两比较。候选召回应只保留少量高置信候选。

推荐候选条件：

1. 实体类型必须一致；
2. 报警码类型只允许非空白字符完全一致，大小写不敏感；
3. 触发规则必须规则签名兼容，例如阈值编号、比较方向、单位不能冲突；
4. 维修方法必须动作类型兼容，例如“检查”和“更换”不要合并；
5. 故障事件必须不存在方向、部位、编号、输入/输出、交流/直流等关键限定冲突；
6. `name_score >= 0.70` 或 `embedding_score >= 0.93`；
7. 每个新 EntityCluster 最多保留 Top 10 个跨文件候选。

跨文件场景不要使用过低的召回阈值。候选越少，越容易控制误合并和运行时间。

## 6. 跨文件综合打分

推荐公式：

```text
score =
  0.45 * name_score
+ 0.35 * embedding_score
+ 0.15 * soft_neighbor_score
+ 0.05 * type_specific_score
```

其中：

- `name_score`：标准名、别名之间的文本相似度；
- `embedding_score`：EntityCluster canonical name + aliases + 证据摘要的语义相似度；
- `soft_neighbor_score`：跨文件关系邻居的语义相似度，不要求邻居已经是同一个节点，只要求邻居名称、类型、关系方向相似；
- `type_specific_score`：针对实体类型的附加规则，例如报警码完全一致、维修动作一致、触发规则阈值一致。

建议自动合并阈值：

```text
auto_merge_threshold = 0.93
```

只有满足以下条件之一才自动合并：

1. `name_score >= 0.92` 且 `embedding_score >= 0.95`，并且无硬冲突；
2. `embedding_score >= 0.97` 且 `soft_neighbor_score >= 0.75`，并且无硬冲突；
3. 报警码完全一致。

否则保留为不同 EntityCluster，不强行合并。

## 7. 硬冲突规则

跨文件聚类必须优先执行硬冲突判断。只要出现硬冲突，直接禁止合并。

常见硬冲突：

- 报警码不同：`3027` 与 `3027-4` 不合并；
- 编号不同：`传感器-1` 与 `传感器-2` 不合并；
- 方向不同：北向/南向、输入/输出、交流/直流、簇内/簇间不合并；
- 部件对象不同：环境温度传感器、冷凝器温度传感器、蒸发器温度传感器不合并；
- 维修动作不同：检查、重启、更换、升级、清洁等动作冲突时不合并；
- 触发规则阈值或比较方向不同：高于/低于、不同阈值编号、不同持续时间不合并；
- 故障状态不同：即将过期、已过期、失效、恢复、未连接等状态冲突时不合并。

## 8. GNR 的“选择文件视图”

GNR 中用户会先选择一个或多个文件。故障树构建和 AI 助手查询时，知识图谱查询范围必须限制为“当前被选择文件支持的视图”。

也就是说，用户选择了哪些 `file_version_id`，GNR 就只能看到这些文件版本支持的：

- `Chunk`
- `Mention`
- `EntityCluster`
- `CLUSTERED_*` 关系
- 对应的 `top_event_catalog`

不能默认查询全库，否则会把其他文件中的故障事件、维修方法和关系混入当前故障树。

推荐判断实体是否可见：

```cypher
MATCH (e:EntityCluster)
WHERE any(fid IN e.source_file_version_ids WHERE fid IN $selected_file_version_ids)
RETURN e;
```

推荐判断关系是否可见：

```cypher
MATCH p=(a:EntityCluster)-[r]->(b:EntityCluster)
WHERE r.relation_level = "clustered"
  AND any(fid IN r.source_file_version_ids WHERE fid IN $selected_file_version_ids)
  AND any(fid IN a.source_file_version_ids WHERE fid IN $selected_file_version_ids)
  AND any(fid IN b.source_file_version_ids WHERE fid IN $selected_file_version_ids)
RETURN p
LIMIT 1000;
```

如果需要 Mention 溯源：

```cypher
MATCH (e:EntityCluster)<-[:RESOLVED_TO]-(m:Mention)-[:EVIDENCED_IN]->(c:Chunk)
WHERE any(fid IN e.source_file_version_ids WHERE fid IN $selected_file_version_ids)
  AND m.file_version_id IN $selected_file_version_ids
  AND c.file_version_id IN $selected_file_version_ids
RETURN e, m, c;
```

MongoDB 中也需要用相同范围过滤：

```javascript
db.top_event_catalog.find({
  source_file_version_ids: { $in: selected_file_version_ids }
})
```

## 9. 推荐落地节奏

第一步：保持当前同文件聚类流程。

第二步：改造 EntityCluster 与关系属性，支持 `source_file_ids`、`source_file_version_ids`、`source_file_scopes`。

第三步：新增跨文件候选召回与保守合并逻辑。

第四步：GNR 查询统一使用“选择文件视图”过滤，不再假设一个 EntityCluster 只属于一个文件。

这个方案能让故障树中同一实体只出现一次，同时仍然保留每个实体和关系来自哪些文件的证据范围。
