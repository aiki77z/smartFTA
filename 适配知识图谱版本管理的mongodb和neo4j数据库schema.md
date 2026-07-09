# 当前系统知识库 Schema


适用代码范围：


## 1. 总体设计

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

collections：

- `files`
- `file_versions`
- `chunks`
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

### 2.4 top_event_catalog

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

