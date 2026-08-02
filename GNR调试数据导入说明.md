# GNR 调试数据导入说明

本文档用于让 GNR 和前端 AI 助手开发人员在本地快速模拟“KB 模块已经完成”的状态：MongoDB 中有文件、版本和 chunks；Neo4j 中有聚类后的实体关系图；MongoDB 的 `top_event_catalog` 中有可供选择的顶事件候选。

以下命令默认工作目录为 `FTA-KB`。

## 1. 准备文件

需要准备两个文件：

```text
HUAWEI LUNA2000-(97KWH-200KWH)系列 工商业构网型储能系统 告警参考_organized.csv
cluster_intermediate.json
```

其中：

- CSV：用于恢复 MongoDB 中的 `files`、`file_versions`、`chunks`。
- `cluster_intermediate.json`：用于导入 Neo4j 中的 `Mention`、`EntityCluster`、实体关系，以及 MongoDB 中的 `top_event_catalog`。

## 2. 配置 .env

在 `FTA-KB/.env` 中配置 MongoDB 和 Neo4j：

```env
MONGO_URI=mongodb://localhost:27017/
MONGO_DB_NAME=smart-fta-test

NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=你的Neo4j密码
NEO4J_DATABASE=neo4j
```

## 3. 导入 CSV 到 MongoDB

运行：

```powershell
.\.venv\Scripts\python.exe import_annotation_csv_to_mongo.py `
  --env-file .env `
  --input-csv "HUAWEI LUNA2000-(97KWH-200KWH)系列 工商业构网型储能系统 告警参考_organized.csv" `
  --file-id huawei_luna2000_alarm_reference `
  --file-version-id huawei_luna2000_alarm_reference_v1 `
  --file-name "HUAWEI LUNA2000-(97KWH-200KWH)系列 工商业构网型储能系统 告警参考"
```

该步骤会写入：

- `files`
- `file_versions`
- `chunks`

默认会清空同一 `file_id + file_version_id` 下的旧 chunks 后重新导入。

## 4. 导入中间产物到 Neo4j 和 MongoDB

运行：

```powershell
.\.venv\Scripts\python.exe import_cluster_intermediate_to_kb.py `
  --env-file .env `
  --intermediate-json cluster_intermediate.json `
  --file-id huawei_luna2000_alarm_reference `
  --file-version-id huawei_luna2000_alarm_reference_v1 `
  --file-name "HUAWEI LUNA2000-(97KWH-200KWH)系列 工商业构网型储能系统 告警参考"
```

该步骤会写入 Neo4j：

- `(:File)`
- `(:Chunk)`
- `(:Mention:FaultEvent)` 等 Mention 层节点
- `(:EntityCluster:FaultEvent)` 等聚类后实体节点
- `(:Mention)-[:EVIDENCED_IN]->(:Chunk)`
- `(:Mention)-[:RESOLVED_TO]->(:EntityCluster)`
- `MENTION_*` 原始关系
- `CLUSTERED_*` 聚类后关系

同时会写入 MongoDB：

- `top_event_catalog`

其中 `top_event_catalog.graph_node_id` 对应 Neo4j 中的 `EntityCluster.cluster_id`。

## 5. Neo4j 查询示例

查看包含 `EntityCluster` 的子图：

```cypher
MATCH p=(:EntityCluster)-[]-()
RETURN p
LIMIT 1000;
```

只看聚类后的实体关系：

```cypher
MATCH p=(:EntityCluster)-[r]->(:EntityCluster)
RETURN p
LIMIT 1000;
```

只看故障触发关系：

```cypher
MATCH p=(:EntityCluster)-[:CLUSTERED_CAUSES]->(:EntityCluster)
RETURN p
LIMIT 1000;
```

从实体簇下钻到 Mention 和 Chunk：

```cypher
MATCH p=(e:EntityCluster)<-[:RESOLVED_TO]-(m:Mention)-[:EVIDENCED_IN]->(c:Chunk)
RETURN p
LIMIT 200;
```

查看导入后的标签统计：

```cypher
MATCH (n)
WHERE n.file_id = "huawei_luna2000_alarm_reference"
  AND n.file_version_id = "huawei_luna2000_alarm_reference_v1"
UNWIND labels(n) AS label
RETURN label, count(*) AS count
ORDER BY label;
```

查看导入后的关系类型统计：

```cypher
MATCH ()-[r]->()
WHERE r.file_id = "huawei_luna2000_alarm_reference"
  AND r.file_version_id = "huawei_luna2000_alarm_reference_v1"
RETURN type(r) AS relation_type, count(*) AS count
ORDER BY relation_type;
```

## 6. MongoDB 检查点

导入后可以检查以下集合：

- `files` 中应存在 `_id = huawei_luna2000_alarm_reference`
- `file_versions` 中应存在 `_id = huawei_luna2000_alarm_reference_v1`
- `chunks` 中应存在该文件版本的 chunks
- `top_event_catalog` 中应存在该文件版本的故障事件候选

说明：MongoDB 中的 chunk 数可能略多于 Neo4j 中的 chunk 数。MongoDB 保存原始 CSV 中全部 chunk；Neo4j 只保存被 Mention 或关系证据引用到的 chunk。
