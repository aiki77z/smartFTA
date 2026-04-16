知识图谱导入与生成链路说明

1. 当前知识图谱导入包
- `output/test1/kg_import_simulated_grouped.json`
- 该文件按 `chunk_id` 分组
- 每条 relation 除基础字段外，还包含：
  - `entity1_props`
  - `entity2_props`

2. Neo4j 导入方式
- 使用脚本：`import_relations_to_neo4j.py`
- 当前脚本会：
  - 导入 `Entity` / `FaultPhenomenon` 节点
  - 导入 `RELATION` 边
  - 把 `entity1_props` / `entity2_props` 写入对应节点属性

命令示例：

```powershell
python import_relations_to_neo4j.py `
  --file output\test1\kg_import_simulated_grouped.json `
  --uri bolt://localhost:7687 `
  --user neo4j `
  --password 你的Neo4j密码 `
  --database neo4j `
  --clear
```

3. 当前默认生成链路
- `prompt -> parse_user_prompt`
- `match_top_event_from_graph`
- `expand_local_fault_subgraph`
- `collect_subgraph_chunks`
- `build_fault_tree_from_subgraph_and_chunks`
- `get_relevant_corrections / repair_fault_tree`
- `validate_full`

4. chunks 的角色
- 不再作为全库召回主入口
- 只从局部子图直接关联的节点和边中收集 `chunk_id`
- 最终用于补全节点 `event.documents`

5. 顶事件候选来源
- 统一来自图谱节点
- 接口：`GET /api/top-events`
- 批量生成：`POST /api/batch/generate-all`

6. 保存逻辑
- 节点属性修改：
  - `description`
  - `errorLevel`
  - `priority`
  - `probability`
  - `showProbability`
  - `rule`
  - `investigateMethod`
  会回写到图谱节点
- 节点名称、结构、gate、link 修改继续写入 corrections
