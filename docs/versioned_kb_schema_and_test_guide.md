# 当前系统知识库 Schema 与测试说明

本文档基于当前仓库代码实现整理，目标是说明：

1. MongoDB 当前有哪些集合、主要字段是什么
2. Neo4j 当前图谱结构是什么
3. 导入完成后需要做什么
4. 如何验证“按文件版本管理 + 会话动态选源 + 运行时拼接子图 + 生成溯源”是否生效

适用代码范围：

- [database.py](/d:/fwwb/fault_tree_system/database.py)
- [main.py](/d:/fwwb/fault_tree_system/main.py)
- [generator.py](/d:/fwwb/fault_tree_system/generator.py)
- [import_relations_to_neo4j.py](/d:/fwwb/fault_tree_system/import_relations_to_neo4j.py)

## 1. 总体设计

当前系统已经切到“只管理文件版本，不管理全局图谱版本表”的模式。

核心原则：

- 文件主键是 `file_id`
- 文件版本主键是 `file_version_id`
- 每个文件版本独立拥有自己的：
  - `chunks`
  - `graph nodes`
  - `graph edges`
  - `top_event_catalog`
- 所有查询都可以按 `selected_file_version_ids` 做严格过滤
- 运行时只在被选中的 `file_version` 范围内拼接子图，不把拼接结果持久化回图数据库

## 2. MongoDB Schema

当前代码里直接使用的集合有：

- `files`
- `file_versions`
- `chunks`
- `entity_reverse_index`
- `top_event_catalog`
- `fault_trees`
- `fault_tree_versions`
- `generation_jobs`
- `generation_job_items`
- `corrections`

### 2.1 files

用途：

- 记录“一个逻辑文件”的稳定身份
- 持有当前激活版本指针

主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `_id` | string | 与 `file_id` 一致 |
| `file_id` | string | 文件稳定 ID |
| `name` | string | 文件名 |
| `normalized_name` | string | 规范化文件名，用于查重 |
| `status` | string | 常见值：`active` / `archived` / `deleted` |
| `current_file_version_id` | string/null | 当前激活版本 |
| `latest_version_no` | int | 最新版本号 |
| `source` | string | 导入来源 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

说明：

- 文件归档后，不会物理删除
- 归档时会把该文件下所有 `file_version` 和相关数据的 `is_active` 置为 `false`

### 2.2 file_versions

用途：

- 记录某个文件的某个具体版本
- 是知识提取、图谱导入、生成选源的核心边界

主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `_id` | string | 与 `file_version_id` 一致 |
| `file_version_id` | string | 文件版本 ID |
| `file_id` | string | 所属文件 ID |
| `file_name` | string | 文件名 |
| `version_no` | int | 版本号 |
| `status` | string | 常见值：`importing` / `active` / `inactive` / `failed` / `archived` |
| `is_active` | bool | 是否为当前激活版本 |
| `source` | string | 导入来源 |
| `metadata` | object | 导入关联文件路径等元数据 |
| `error` | string | 导入失败时记录错误 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

说明：

- 同一个 `file_id` 只允许一个 `file_version_id` 处于 `is_active = true`
- 新版本激活后，旧版本会自动切为 `inactive`

### 2.3 chunks

用途：

- 保存文档切分结果
- 为生成阶段提供证据文本

主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `chunk_id` | string/int | 原始 chunk 编号 |
| `id` | string/int | 与 `chunk_id` 兼容 |
| `chunk_uid` | string | 组合键，格式 `file_version_id::chunk_id` |
| `file_id` | string | 文件 ID |
| `file_version_id` | string | 文件版本 ID |
| `is_active` | bool | 当前是否激活 |
| `file` | string | 原始文件名 |
| `chunk_name` | string | chunk 标题 |
| `section_path` | string | 文档路径 |
| `source` | string | 页码或来源标识 |
| `content` | string | chunk 正文 |
| `entities` | array | chunk 内实体列表 |

说明：

- 当前系统会优先使用 `chunk_uid`
- 这样可以避免不同文件版本中 `chunk_id` 重号导致混用

### 2.4 entity_reverse_index

用途：

- 保存实体到 chunk 的反向索引
- 用于按实体名快速召回 chunk

主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `entity_name` | string | 实体名 |
| `chunk_ids` | array | 命中的 chunk 列表 |
| `count` | int | 出现次数 |
| `file_id` | string | 文件 ID |
| `file_version_id` | string | 文件版本 ID |
| `is_active` | bool | 是否激活 |

说明：

- 这个集合也已经加上了版本边界
- 查询时会叠加 `selected_file_version_ids`

### 2.5 top_event_catalog

新增字段（2026-04 prompt 解析改造）：

- `display_name`: 前端展示用标准名称，当前与 `name` 保持一致
- `semantic_text`: 顶事件语义匹配文本
- `embedding`: 顶事件向量，供当前 scope 内候选召回使用
- `embedding_model`: 当前 embedding 模型名
- `embedding_updated_at`: 向量更新时间

新增行为：

- `prompt` 先由 LLM 提取 `requested_top_event` 和 `requirements`
- 当前 scope 下如果没有 `top_event_catalog`，后端会先按选中的 `file_version_id` 自动重建目录
- 如果没有精确命中，则在当前 scope 的 `top_event_catalog` 内做向量/语义匹配，并把候选返回给用户确认
- 若未配置 `EMBEDDING_MODEL` 或向量请求失败，则回退为基于名称的 lexical 候选排序
- 若使用阿里云百炼 OpenAI 兼容 embedding，建议配置 `EMBEDDING_BATCH_SIZE=10`

用途：

- 保存“每个文件版本自己的顶事件目录”
- 会话里再根据 `selected_file_version_ids` 合并返回

主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `_id` | string | 格式 `file_version_id::normalized_name` |
| `name` | string | 顶事件名称 |
| `normalized_name` | string | 标准化名称 |
| `aliases` | array | 别名 |
| `normalized_aliases` | array | 规范化别名 |
| `file_id` | string | 文件 ID |
| `file_version_id` | string | 文件版本 ID |
| `graph_node_id` | string | 对应 Neo4j 根节点 ID |
| `source_chunk_ids` | array | 来源 chunk 或 chunk_ref 列表 |
| `is_active` | bool | 当前是否激活 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

说明：

- 顶事件列表已经不是“全库唯一目录”
- 同名顶事件可以在多个 `file_version` 中各自存在
- 会话里只会返回当前选源范围内的顶事件

### 2.6 fault_trees

新增字段（2026-04 prompt 解析改造）：

- `requested_top_event`: 用户原始请求中的顶事件
- `resolved_top_event`: 系统精确命中或用户确认后的显示名
- `graph_node_id`: 用户确认后锁定的图节点 ID；若为精确命中可为空

用途：

- 保存一棵故障树的元信息

主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `_id` | string | `tree_id` |
| `top_event` | string | 顶事件 |
| `catalog_name` | string | 目录中的标准名称 |
| `normalized_top_event` | string | 标准化顶事件 |
| `query_aliases` | array | 查询用别名 |
| `source_chunk_ids` | array | 当前版本证据 chunk 列表 |
| `source_file_version_ids` | array | 本树使用的文件版本集合 |
| `source_scope_key` | string | 对 `source_file_version_ids` 排序后拼出的 scope key |
| `job_id` | string | 生成任务 ID |
| `job_item_id` | string | 生成任务条目 ID |
| `current_version` | int | 当前树版本号 |
| `status` | string | `generating` / `ai_generated` / `expert_modified` / `rolled_back` |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

说明：

- 树复用现在会按 `source_scope_key` 隔离
- 同一个顶事件，在不同 `selected_file_version_ids` 下可以得到不同树版本，不会串用

### 2.7 fault_tree_versions

新增字段（2026-04 prompt 解析改造）：

- `requested_top_event`
- `resolved_top_event`
- `normalized_top_event`

用途：

- 保存故障树的每个版本快照
- 是生成结果溯源的核心落点

主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `tree_id` | string | 树 ID |
| `version` | int | 版本号 |
| `is_ai_generated` | bool | 是否 AI 生成 |
| `editor` | string | 编辑者 |
| `description` | string | 版本说明 |
| `source_scope_key` | string | 生成时的选源 scope |
| `source_file_version_ids` | array | 本次使用的文件版本集合 |
| `evidence_chunk_ids` | array | 本次使用的 chunk 集合 |
| `subgraph_node_ids` | array | 本次运行时子图节点集合 |
| `tree_data` | object | 故障树 JSON |
| `created_at` | datetime | 创建时间 |

这三个字段是本次改造后最关键的溯源字段：

- `source_file_version_ids`
- `evidence_chunk_ids`
- `subgraph_node_ids`

### 2.8 generation_jobs

用途：

- 保存批量生成任务主表

主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `_id` | string | `job_id` |
| `job_type` | string | `single_generate` / `batch_generate_all` |
| `top_event` | string/null | 单任务时可能有值 |
| `status` | string | `pending` / `running` / `completed` / `partial_failed` / `failed` |
| `total` | int | 总条目数 |
| `success` | int | 成功数 |
| `failed` | int | 失败数 |
| `running` | int | 运行中数 |
| `pending` | int | 排队数 |
| `metadata` | object | 额外信息 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |
| `started_at` | datetime | 启动时间 |
| `finished_at` | datetime | 结束时间 |
| `duration_seconds` | float | 耗时 |

### 2.9 generation_job_items

新增字段（2026-04 prompt 解析改造）：

- `requested_top_event`
- `resolved_top_event`
- `graph_node_id`

说明：

- 单次生成任务会把“用户原始顶事件 / 最终生成顶事件 / 用户确认后的图节点”一并落库
- 如果前端先调用顶事件解析接口，再把候选确认结果传给 `generate`，后端会直接使用该 `graph_node_id` 生成，避免二次歧义匹配

用途：

- 保存每个具体生成项
- 用于前端轮询和任务去重

主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `_id` | string | `item_id` |
| `job_id` | string | 所属任务 |
| `top_event` | string | 顶事件 |
| `normalized_top_event` | string | 标准化顶事件 |
| `aliases` | array | 别名 |
| `source_chunk_ids` | array | 输入 chunk 提示 |
| `source_file_version_ids` | array | 当前选中的文件版本集合 |
| `source_scope_key` | string | 选源 scope key |
| `requirements` | string | 生成要求 |
| `status` | string | `pending` / `running` / `success` / `failed` |
| `progress` | int | 进度 |
| `stage` | string | 阶段名 |
| `message` | string | 当前说明 |
| `events` | array | 实时日志流 |
| `tree_id` | string | 生成出的树 ID |
| `error` | string | 错误信息 |
| `execution_owner` | string | 执行者 |
| `metadata` | object | 扩展数据 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |
| `started_at` | datetime | 启动时间 |
| `finished_at` | datetime | 结束时间 |
| `duration_seconds` | float | 耗时 |

### 2.10 corrections

用途：

- 保存 AI 版和专家版之间的结构差异
- 用于后续纠偏学习

当前字段摘要：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `tree_id` | string | 树 ID |
| `ai_version` | int | AI 版本号 |
| `expert_version` | int | 专家版本号 |
| `top_event` | string | 顶事件 |
| `node_name` | string | 关联节点名 |
| `node_type` | string | 节点类型 |
| `correction_type` | string | 修正类型 |
| `detail` | object | 详细修正内容 |
| `semantic_text` | string | 检索文本 |
| `embedding` | array | 向量 |
| `created_at` | datetime | 创建时间 |

说明：

- 节点属性类修改现在优先直接回写对应 `file_version` 的图节点属性
- 结构性修改仍保存在 `corrections` 里，不直接改图结构

## 3. MongoDB 关键索引

代码里已经显式创建了以下关键索引：

- `files.normalized_name`
- `file_versions.(file_id, version_no desc)`
- `file_versions.file_version_id`
- `file_versions.(is_active, status)`
- `chunks.chunk_uid`
- `chunks.(file_version_id, chunk_id)`
- `chunks.(file_id, file_version_id, is_active)`
- `top_event_catalog.normalized_name`
- `top_event_catalog.normalized_aliases`
- `top_event_catalog.(file_version_id, normalized_name)`
- `generation_job_items.(job_id, status)`
- `generation_job_items.(normalized_top_event, status)`
- `generation_job_items.(source_scope_key, normalized_top_event, status)`

## 4. Neo4j Schema

当前图谱主要由三类节点和两类关系组成。

### 4.1 节点类型

#### `:Entity`

用途：

- 故障现象
- 组件
- 参数
- 方法
- 以及故障树中的普通故障节点

主键约束：

- `(name, entity_type, file_version_id)` 唯一

主要属性：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 节点名 |
| `entity_type` | string | 实体类型 |
| `file_id` | string | 文件 ID |
| `file_version_id` | string | 文件版本 ID |
| `is_active` | bool | 当前是否激活 |
| `normalized_name` | string | 标准化名称 |
| `description` | string | 描述 |
| `errorLevel` | string | 错误级别 |
| `priority` | number | 优先级 |
| `probability` | number | 概率 |
| `showProbability` | bool | 是否展示概率 |
| `rule` | string | 规则 |
| `investigateMethod` | string | 排查方法 |
| `documents` | string(JSON) | 这里只保存 `chunk_id` 列表结构 |
| `source_chunk_ids` | array/string | 来源 chunk 列表 |
| `support_count` | int | 支持计数 |

说明：

- 代码在读取时会把 `documents` 解析成对象列表
- 并额外生成 `source_chunk_refs = file_version_id::chunk_id`

#### `:Chunk`

用途：

- 表示知识图谱中被引用到的 chunk 节点

主键约束：

- `(file_version_id, chunk_id)` 唯一

主要属性：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `chunk_id` | string | chunk 编号 |
| `file_id` | string | 文件 ID |
| `file_version_id` | string | 文件版本 ID |
| `file_name` | string | 文件名 |
| `is_active` | bool | 是否激活 |
| `updated_at` | datetime | 更新时间 |

#### `:LogicGate`

用途：

- 表示 AND 逻辑节点

说明：

- 运行时识别时，如果节点带 `LogicGate` 标签或 `node_type = AND`，则会被视作与门
- 目前 OR 不单独落图节点，而是在树骨架阶段由“多子节点且非 AND”推断为 `OR`

### 4.2 关系类型

#### `[:RELATION]`

用途：

- 实体之间的业务关系
- 当前故障树主链路主要依赖 `relation_type = '触发'`

关键属性：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `relation_type` | string | 关系类型，如 `触发` |
| `chunk_id` | string | 来源 chunk |
| `file_id` | string | 文件 ID |
| `file_version_id` | string | 文件版本 ID |
| `is_active` | bool | 是否激活 |
| `source_chunk_ids` | array/string | 支撑 chunk 列表 |
| `support_count` | int | 支持计数 |
| `updated_at` | datetime | 更新时间 |

说明：

- 运行时扩展局部子图时，会严格按 `file_version_id IN selected_file_version_ids` 过滤

#### `[:MENTIONED_IN]`

用途：

- 实体节点指向 chunk 节点

关键属性：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `file_version_id` | string | 文件版本 ID |
| `is_active` | bool | 是否激活 |
| `updated_at` | datetime | 更新时间 |

## 5. Neo4j 当前约束

当前导入脚本会确保以下约束存在：

```cypher
CREATE CONSTRAINT entity_identity IF NOT EXISTS
FOR (e:Entity)
REQUIRE (e.name, e.entity_type, e.file_version_id) IS UNIQUE
```

```cypher
CREATE CONSTRAINT chunk_identity IF NOT EXISTS
FOR (c:Chunk)
REQUIRE (c.file_version_id, c.chunk_id) IS UNIQUE
```

## 6. 导入完成后要做什么

如果你是走 API 导入：

- `POST /api/integration/import-knowledge-artifacts`

那么系统已经会自动做这几件事：

1. 创建 `file_id` / `file_version_id`
2. 导入 chunks
3. 导入 entity reverse index
4. 导入 Neo4j relations
5. 重建该 `file_version` 的 `top_event_catalog`
6. 激活新版本并失活旧版本

如果你是手工导入 MongoDB 和 Neo4j，那么导入完成后必须补做这两步：

1. 为每个 `file_version_id` 重建 `top_event_catalog`
2. 激活对应版本

示例：

```powershell
python -c "from database import rebuild_top_event_catalog_for_file_version, activate_file_version; rebuild_top_event_catalog_for_file_version('fv_test_part1_cleaned_v1'); activate_file_version('file_test_part1_cleaned','fv_test_part1_cleaned_v1')"
python -c "from database import rebuild_top_event_catalog_for_file_version, activate_file_version; rebuild_top_event_catalog_for_file_version('fv_test_part2_cleaned_v1'); activate_file_version('file_test_part2_cleaned','fv_test_part2_cleaned_v1')"
```

然后建议做 4 个检查。

### 6.1 检查 file 与 file_version

目标：

- `files.current_file_version_id` 正确
- `file_versions.is_active` 状态正确

### 6.2 检查 top_event_catalog

目标：

- 每个 `file_version` 都有自己的顶事件目录
- 同名顶事件在不同版本可共存

### 6.3 检查 Neo4j 数据是否带版本属性

目标：

- `Entity` / `Chunk` / `RELATION` 都有 `file_id`、`file_version_id`、`is_active`

### 6.4 检查生成接口能否按选源工作

目标：

- 同一个顶事件，在不同 `selected_file_version_ids` 下召回的子图和 chunk 不同

## 7. 导入后的基础验证

### 7.1 列出当前顶事件目录

不加选源，默认会看当前 active 版本：

```powershell
curl "http://127.0.0.1:8000/api/catalog/top-events"
```

只看某个文件版本：

```powershell
curl "http://127.0.0.1:8000/api/catalog/top-events?selected_file_version_ids=fv_test_part1_cleaned_v1"
```

同时看多个文件版本：

```powershell
curl "http://127.0.0.1:8000/api/catalog/top-events?selected_file_version_ids=fv_test_part1_cleaned_v1,fv_test_part2_cleaned_v1"
```

你重点要看返回里的：

- `items[*].file_version_id`
- `items[*].graph_node_id`
- 同名顶事件是否只在你选中的版本范围内出现

### 7.2 预览批量可生成顶事件

```powershell
curl -X POST "http://127.0.0.1:8000/api/batch/preview-top-events" `
  -H "Content-Type: application/json" `
  -d "{\"selected_file_version_ids\":[\"fv_test_part1_cleaned_v1\"]}"
```

返回里重点看：

- `items[*].name`
- `items[*].file_version_ids`
- 是否只来自当前选中的版本

### 7.3 调试图谱召回

这是最适合验证“动态选源 + 运行时拼接子图”的接口。

```powershell
curl -X POST "http://127.0.0.1:8000/api/debug/graph-recall" `
  -H "Content-Type: application/json" `
  -d "{\"top_event\":\"你的顶事件名\",\"selected_file_version_ids\":[\"fv_test_part1_cleaned_v1\"]}"
```

返回里重点看这几块：

- `query.selected_file_version_ids`
- `graph_match.matched_nodes`
- `subgraph.nodes`
- `subgraph.edges`
- `chunk_recall.chunk_ids`
- `chunk_recall.chunks[*].file_version_id`

验收标准：

- 返回的所有节点、边、chunk 都只能来自 `fv_test_part1_cleaned_v1`

再换另一个范围测试：

```powershell
curl -X POST "http://127.0.0.1:8000/api/debug/graph-recall" `
  -H "Content-Type: application/json" `
  -d "{\"top_event\":\"你的顶事件名\",\"selected_file_version_ids\":[\"fv_test_part2_cleaned_v1\"]}"
```

如果两边召回不同，说明“会话动态选源”已经生效。

## 8. 生成测试

### 8.1 单个生成

```powershell
curl -X POST "http://127.0.0.1:8000/api/tree/generate" `
  -H "Content-Type: application/json" `
  -d "{\"prompt\":\"分析 XXX 故障\",\"selected_file_version_ids\":[\"fv_test_part1_cleaned_v1\"]}"

如果当前 scope 下不是精确命中，建议先调用：

```powershell
curl -X POST "http://127.0.0.1:8000/api/tree/resolve-top-event" `
  -H "Content-Type: application/json" `
  -d "{\"prompt\":\"请为我生成顶事件为XXX的故障树，要求给出主要原因\",\"selected_file_version_ids\":[\"fv_test_part1_cleaned_v1\"],\"candidate_limit\":10}"
```

返回 `status = need_user_confirmation` 时，前端应把用户最终选择的 `confirmed_top_event`、`confirmed_normalized_top_event`、`confirmed_graph_node_id` 再传回 `POST /api/tree/generate`。
```

返回后记录：

- `job_id`
- `item_id`
- `tree_id`

然后轮询：

```powershell
curl "http://127.0.0.1:8000/api/batch/job-item/你的item_id"
curl "http://127.0.0.1:8000/api/tree/你的tree_id"
```

### 8.2 批量生成

```powershell
curl -X POST "http://127.0.0.1:8000/api/batch/generate-all" `
  -H "Content-Type: application/json" `
  -d "{\"selected_file_version_ids\":[\"fv_test_part1_cleaned_v1\",\"fv_test_part2_cleaned_v1\"]}"
```

轮询任务：

```powershell
curl "http://127.0.0.1:8000/api/batch/job/你的job_id"
```

## 9. 如何验证溯源字段

生成一棵树后，调用：

```powershell
curl "http://127.0.0.1:8000/api/tree/你的tree_id/version/1"
```

重点检查版本文档里是否存在：

- `source_file_version_ids`
- `evidence_chunk_ids`
- `subgraph_node_ids`

验收标准：

- `source_file_version_ids` 必须等于本次请求传入的 `selected_file_version_ids`
- `evidence_chunk_ids` 必须只包含当前 scope 内的 chunk
- `subgraph_node_ids` 必须只包含当前 scope 内的图节点

## 10. 推荐回归测试清单

建议最少做下面 6 组测试。

### 用例 1：单版本顶事件列表

输入：

- `selected_file_version_ids = [fv_test_part1_cleaned_v1]`

预期：

- 只返回 `test_part1` 版本内顶事件

### 用例 2：多版本顶事件合并

输入：

- `selected_file_version_ids = [fv_test_part1_cleaned_v1, fv_test_part2_cleaned_v1]`

预期：

- 返回两个版本目录的并集
- 同名事件可以被合并展示，但返回中应保留对应 `file_version_ids`

### 用例 3：图谱召回严格过滤

输入：

- 调用 `/api/debug/graph-recall`

预期：

- 返回子图的节点、边、chunk 都带对应 `file_version_id`
- 不出现未选中的版本

### 用例 4：单版本生成溯源

输入：

- `selected_file_version_ids = [fv_test_part1_cleaned_v1]`

预期：

- 生成版本记录中 `source_file_version_ids = ['fv_test_part1_cleaned_v1']`

### 用例 5：多版本运行时拼接

输入：

- `selected_file_version_ids = [fv_test_part1_cleaned_v1, fv_test_part2_cleaned_v1]`

预期：

- 若两个版本中都有同名顶事件，则 `graph_match.matched_nodes` 会出现多个根
- 最终子图是运行时合并结果
- 不会在数据库里生成新的“拼接图谱版本”

### 用例 6：切换选源后结果隔离

步骤：

1. 先用 `selected_file_version_ids = [fv_test_part1_cleaned_v1]` 生成
2. 再用 `selected_file_version_ids = [fv_test_part2_cleaned_v1]` 生成同名顶事件

预期：

- 两次结果的 `source_scope_key` 不同
- 任务去重不会串
- 树复用不会串
- 溯源字段不同

## 11. 当前最值得你确认的点

你导入完后，最先确认这 3 件事就够了：

1. `top_event_catalog` 是否已经按 `file_version_id` 建好
2. `/api/debug/graph-recall` 返回的 chunk 和图节点是否都带正确的 `file_version_id`
3. `/api/tree/{tree_id}/version/{version}` 里是否能看到 `source_file_version_ids`、`evidence_chunk_ids`、`subgraph_node_ids`

如果这 3 个都对，说明这次改造的主链路基本已经通了。
