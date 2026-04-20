# Worktree分支代码运行方法

本文档说明当前项目的 worktree 结构、每个分支的职责、如何分别运行，以及三个分支之间是如何联动的。

## 1. 当前 worktree 结构

当前项目已经拆成 3 个独立 worktree：

1. `D:\fwwb\fault_tree_visual`
   分支：`main`
   职责：前端页面与交互

2. `D:\fwwb\fault_tree_system`
   分支：`generate-fta`
   职责：故障树自动生成后端

3. `D:\fwwb\knowledge_base_construction`
   分支：`knowledge_base_construction`
   职责：知识库构建、实体提取、关系提取、知识产物同步

这三个 worktree 分别负责不同能力，避免把前端、故障树生成、知识库构建混在同一个工作目录里。

## 2. 各分支职责

### 2.1 `main` 分支

路径：`D:\fwwb\fault_tree_visual`

职责：

- 提供前端页面
- 对接 `generate-fta` 后端接口
- 展示故障树、任务进度、顶事件目录等

### 2.2 `generate-fta` 分支

路径：`D:\fwwb\fault_tree_system`

职责：

- 故障树智能生成
- 顶事件发现与标准化
- 任务管理与进度查询
- 复用已有故障树
- 图谱召回、图谱草稿生成、三层图谱增强召回
- MongoDB / Neo4j 数据消费

该分支是当前故障树自动生成系统的主后端。

### 2.3 `knowledge_base_construction` 分支

路径：`D:\fwwb\knowledge_base_construction`

职责：

- PDF 转 Markdown
- Markdown 分块
- 实体提取
- 关系提取
- 产出标准知识文件
- 在成功后自动同步这些知识产物到 `generate-fta`

该分支不再重复保留故障树自动生成代码，只负责知识构建这一段。

## 3. 三个分支之间的真实连接方式

当前已经不是“手工跑完一边，再自己敲几条导入命令”的关系，而是服务到服务的联动：

1. `knowledge_base_construction` 生成标准产物：
   - `*_chunks.json`
   - `*_entities_merged.json`
   - `*_relations.jsonl`

2. 生成成功后，`knowledge_base_construction` 会自动调用 `generate-fta` 的接口：
   - `POST /api/integration/import-knowledge-artifacts`

3. `generate-fta` 收到请求后会自动：
   - 导入 chunks 到 MongoDB
   - 导入 entity reverse index 到 MongoDB
   - 导入 relations 到 Neo4j

4. 导入完成后，就可以直接在 `generate-fta` 上：
   - 预览顶事件
   - 调试图谱召回
   - 批量生成故障树
   - 单树生成

因此，当前联动链路是：

`knowledge_base_construction -> generate-fta -> main`

也就是：

`知识构建 -> 故障树生成后端 -> 前端展示`

## 4. 每个分支如何运行

### 4.1 运行前端 `main`

路径：

`D:\fwwb\fault_tree_visual`

通常启动方式：

```powershell
cd D:\fwwb\fault_tree_visual
npm install
npm run dev
```

如果你们项目实际不是 `npm run dev`，以该前端仓库自己的脚本为准。

### 4.2 运行故障树后端 `generate-fta`

路径：

`D:\fwwb\fault_tree_system`

启动方式：

```powershell
cd D:\fwwb\fault_tree_system
uvicorn main:app --reload --port 8000
```

主要接口：

- `POST /api/tree/generate`
- `POST /api/tree/resolve-top-event`
- `POST /api/batch/preview-top-events`
- `POST /api/batch/generate-all`
- `POST /api/debug/graph-recall`
- `GET /api/batch/job/{job_id}`
- `GET /api/catalog/top-events`
- `POST /api/integration/import-knowledge-artifacts`

### 4.3 运行知识库构建后端 `knowledge_base_construction`

路径：

`D:\fwwb\knowledge_base_construction`

启动方式：

```powershell
cd D:\fwwb\knowledge_base_construction
uvicorn main:app --reload --port 8010
```

主要接口：

- `POST /api/kb/jobs/run`
- `GET /api/kb/jobs/{job_id}`
- `POST /api/kb/neo4j/import`

## 5. 推荐启动顺序

建议按下面顺序启动：

1. 启动 `generate-fta`
2. 启动 `knowledge_base_construction`
3. 启动 `main` 前端

原因是：

- `knowledge_base_construction` 默认会在任务完成后自动调用 `generate-fta`
- 如果 `generate-fta` 没启动，知识构建任务虽然可以完成，但同步会失败

## 6. `generate-fta` 当前如何接收知识产物

`generate-fta` 新增了一个专门用于联动导入的接口：

`POST /api/integration/import-knowledge-artifacts`

请求示例：

```json
{
  "chunks_file": "D:\\path\\to\\test_cleaned_chunks.json",
  "entities_file": "D:\\path\\to\\test_cleaned_entities_merged.json",
  "relations_file": "D:\\path\\to\\test_cleaned_relations.jsonl",
  "clear_graph": true,
  "import_relations": true,
  "source": "knowledge_base_construction"
}
```

接口行为：

1. 读取 chunks 文件并导入 MongoDB
2. 读取实体合并文件并导入 entity reverse index
3. 读取关系文件并导入 Neo4j

成功后即可直接继续做故障树生成。

## 7. `knowledge_base_construction` 正常生成模式

如果你要从 PDF 重新跑完整知识构建流程，可调用：

```http
POST http://127.0.0.1:8010/api/kb/jobs/run
```

请求示例：

```json
{
  "pdf_path": "D:\\docs\\manual.pdf",
  "output_dir": "./output",
  "chunk_size": 800,
  "skip_mineru": false,
  "skip_entity": false,
  "skip_relation": false,
  "print_raw_text": false,
  "sync_to_generate_fta": true,
  "generate_fta_base_url": "http://127.0.0.1:8000",
  "clear_graph_before_import": true
}
```

任务运行成功后：

- 会在输出目录生成标准产物
- 并自动同步到 `generate-fta`

可通过：

```http
GET http://127.0.0.1:8010/api/kb/jobs/{job_id}
```

查看状态。

关键状态字段：

- `status`
- `sync_status`
- `sync_response`
- `sync_error`

## 8. `knowledge_base_construction` 的 import-only 模式

现在已经把你要的“直接导入不生成”能力加回来了。

### 8.1 CLI 模式

路径：

`D:\fwwb\knowledge_base_construction\run.py`

新增参数：

- `--import-only-dir`
- `--pdf-stem`

示例：

```powershell
cd D:\fwwb\knowledge_base_construction
python run.py --import-only-dir D:\existing_artifacts --pdf-stem test_cleaned
```

含义：

- 直接复用 `D:\existing_artifacts` 下已经存在的知识产物
- 跳过 PDF 转换、分块、实体提取、关系提取等所有生成步骤
- 只校验并暴露这些产物路径

当前会寻找的文件是：

- `test_cleaned_chunks.json`
- `test_cleaned_entities_merged.json`
- `test_cleaned_relations.jsonl`

如果目录里只有一个 `*_chunks.json`，也可以不传 `--pdf-stem`，系统会自动推断。

### 8.2 服务模式

`POST /api/kb/jobs/run` 现在也支持 import-only 模式。

请求示例：

```json
{
  "import_only_dir": "D:\\existing_artifacts",
  "pdf_stem": "test_cleaned",
  "skip_entity": false,
  "skip_relation": false,
  "sync_to_generate_fta": true,
  "generate_fta_base_url": "http://127.0.0.1:8000",
  "clear_graph_before_import": true
}
```

这时行为是：

1. 不重新生成 chunks / entities / relations
2. 直接复用已有产物
3. 自动同步到 `generate-fta`

这个模式非常适合：

- 已经做过知识抽取，只想重新导入
- 从别人产出的目录直接接入
- 排查同步问题时跳过大模型生成步骤

## 9. 典型联动流程

### 9.1 从 PDF 到故障树

1. 启动 `generate-fta`
2. 启动 `knowledge_base_construction`
3. 调 `POST /api/kb/jobs/run` 跑完整知识构建
4. 等任务状态变成：
   - `status = success`
   - `sync_status = success`
5. 切到 `generate-fta` 调：
   - `POST /api/batch/preview-top-events`
   - 或 `POST /api/batch/generate-all`

### 9.2 从已有知识产物直接接入

1. 启动 `generate-fta`
2. 启动 `knowledge_base_construction`
3. 调 `POST /api/kb/jobs/run`，只传：
   - `import_only_dir`
   - `pdf_stem`
4. 等待同步成功
5. 再去 `generate-fta` 做顶事件预览或批量生成

## 10. 常见问题

### 10.1 为什么知识构建任务成功了，但 `sync_status = failed`

一般是：

- `generate-fta` 没启动
- `generate_fta_base_url` 写错
- `generate-fta` 的 MongoDB / Neo4j 环境没配好
- `relations_file` 存在，但 `generate-fta` 没配置 `NEO4J_PASSWORD`

### 10.2 为什么 import-only 模式报缺少文件

因为它要求目录下存在标准产物命名：

- `{pdf_stem}_chunks.json`
- `{pdf_stem}_entities_merged.json`
- `{pdf_stem}_relations.jsonl`

如果文件名不符合，就需要改名或显式传正确的 `pdf_stem`。

### 10.3 三个 worktree 是否互相覆盖

不会。

它们是同一个 Git 仓库的不同 worktree，但各自工作目录独立：

- `main` 不影响 `generate-fta`
- `generate-fta` 不影响 `knowledge_base_construction`
- `knowledge_base_construction` 也不再重复保存故障树自动生成代码

## 11. 当前建议

如果你现在要实际跑起来，建议按这个顺序：

1. `D:\fwwb\fault_tree_system`
   启动 `generate-fta`
2. `D:\fwwb\knowledge_base_construction`
   启动知识库构建服务
3. 先用 import-only 模式跑一次
   确认自动同步链路没问题
4. 再尝试完整 PDF 抽取模式
5. 最后启动前端联调

这样最稳，也最容易定位问题出在哪一层。
