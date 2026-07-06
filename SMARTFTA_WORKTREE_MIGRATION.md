# smartFTA 从零拉取与运行指南

这份文档给第一次接触项目的同学使用。目标是在一个新目录里拉取代码，用 Git worktree 组织三个主要模块，并把项目跑起来。

远程仓库：

```text
https://github.com/aiki77z/smartFTA.git
```

## 1. 项目目录

最终建议目录长这样：

```text
smartFTA/
  fta-visual-system/   # 前端，来自 main 分支中的子目录
  FTA-KB/              # 知识库构建服务，来自 kb-v2 分支
  FTA-GNR/             # 故障树生成服务，来自 generate-fta 分支
```

分支对应关系：

```text
main         -> fta-visual-system/
kb-v2        -> FTA-KB/
generate-fta -> FTA-GNR/
```

## 2. 准备环境

建议提前安装：

```text
Git
Node.js 20+
Python 3.11
MongoDB
Neo4j
```

其中 MongoDB 用于保存知识分块、故障树、任务记录等数据；Neo4j 用于保存知识图谱关系。

## 3. 拉取代码

下面示例把项目放到：

```text
E:\Desktop\study\A15-Project\smartFTA
```

可以按自己的目录替换。

```powershell
cd E:\Desktop\study\A15-Project
git clone https://github.com/aiki77z/smartFTA.git smartFTA
cd smartFTA
git fetch origin
```

添加两个 worktree：

```powershell
git worktree add -b kb-v2 .\FTA-KB origin/kb-v2
git worktree add -b generate-fta .\FTA-GNR origin/generate-fta
```

检查结果：

```powershell
git worktree list
```

应看到类似：

```text
...\smartFTA          main
...\smartFTA\FTA-KB   kb-v2
...\smartFTA\FTA-GNR  generate-fta
```

为了让主 worktree 不把 `FTA-KB/`、`FTA-GNR/` 当成未跟踪目录，可以把下面内容写入 `smartFTA/.git/info/exclude`：

```gitignore
/FTA-KB/
/FTA-GNR/
/.env
```

PowerShell 示例：

```powershell
Add-Content .git\info\exclude "`n/FTA-KB/`n/FTA-GNR/`n/.env"
```

## 4. 配置环境变量

项目里有多个服务，各自读取自己的 `.env`。这些文件不要提交到 Git。

### FTA-GNR 配置

创建：

```text
smartFTA/FTA-GNR/.env
```

内容参考.env.example。

如果前端不是 Vite 默认的 `http://127.0.0.1:5173`，还需要在 `FTA-GNR/.env` 中配置高保真图片导出的前端访问地址：

```env
FTA_EXPORT_BASE_URL=http://127.0.0.1:5173
```

### FTA-KB 配置

创建：

```text
smartFTA/FTA-KB/.env
```

内容参考.env.example。

### fta-visual-system/fta-ai-service 配置

创建：

```text
smartFTA/fta-visual-system/fta-ai-service/.env
```

内容参考.env.example。

## 5. 安装依赖

### 前端

```powershell
cd E:\Desktop\study\A15-Project\smartFTA\fta-visual-system
npm install
```

### FTA-GNR

如果使用虚拟环境，先创建并激活：

```powershell
cd E:\Desktop\study\A15-Project\smartFTA\FTA-GNR
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

### FTA-KB

如果使用虚拟环境，先创建并激活：

```powershell
cd E:\Desktop\study\A15-Project\smartFTA\FTA-KB
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
```

如果要从 PDF 重新抽取知识，`FTA-KB` 还需要 MinerU。继续在同一个虚拟环境里安装：

```powershell
python -m pip install --upgrade pip
python -m pip install -U "mineru[all]"
```

如果下载较慢，可以使用国内镜像：

```powershell
python -m pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple
python -m pip install -U "mineru[all]" -i https://mirrors.aliyun.com/pypi/simple
```

安装后检查命令是否可用：

```powershell
mineru --help
```

首次运行 MinerU 可能会下载模型文件，时间会比较久。

### AI 编辑服务

如果使用虚拟环境，先创建并激活：

```powershell
cd E:\Desktop\study\A15-Project\smartFTA\fta-visual-system\fta-ai-service
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
```

## 6. 启动项目

启动前先确认 MongoDB 和 Neo4j 已运行。(MongoDB:Create Database → 按群里的图片在该Database下建表)(Neo4j:创建新的instance即可)

建议按这个顺序启动。

### 1. 启动 FTA-GNR

```powershell
cd E:\Desktop\study\A15-Project\smartFTA\FTA-GNR
.\.venv\Scripts\activate
uvicorn main:app --reload --port 8000
```

服务地址：

```text
http://localhost:8000
```

### 2. 启动 FTA-KB

新开一个终端：

```powershell
cd E:\Desktop\study\A15-Project\smartFTA\FTA-KB
.\.venv\Scripts\activate
uvicorn main:app --reload --port 8010
```

服务地址：

```text
http://localhost:8010
```

### 3. 启动 AI 编辑服务

新开一个终端：

```powershell
cd E:\Desktop\study\A15-Project\smartFTA\fta-visual-system\fta-ai-service
.\.venv\Scripts\activate
uvicorn app.main:app --host 0.0.0.0 --port 8020 --reload
```

服务地址：

```text
http://localhost:8020
```

### 4. 启动前端

新开一个终端：

```powershell
cd E:\Desktop\study\A15-Project\smartFTA\fta-visual-system
npm run dev
```

Vite 默认地址通常是：

```text
http://localhost:5173
```

`FTA-GNR` 的高保真图片导出会通过 Playwright 访问前端页面并截图，因此导出前需要确保前端服务正在运行，且 `FTA_EXPORT_BASE_URL` 指向当前前端地址。

## 7. 快速验证

后端 API 文档：

```text
http://localhost:8000/docs
http://localhost:8010/docs
http://localhost:8020/docs
```

前端构建检查：

```powershell
cd E:\Desktop\study\A15-Project\smartFTA\fta-visual-system
npm run build
```

如果前端页面能打开，但请求失败，优先检查：

```text
smartFTA/fta-visual-system/.env
smartFTA/fta-visual-system/fta-ai-service/.env
smartFTA/FTA-GNR/.env
smartFTA/FTA-KB/.env
```

## 8. 本地数据说明

Git 仓库只保存代码和少量示例文件，不保存本地运行产生的大量数据。

常见本地数据包括：

```text
FTA-KB/output/              # KB 抽取产物
FTA-KB/uploads/             # 前端上传的 PDF
FTA-GNR/multi/              # 本地批处理或测试数据
fta-visual-system/exploded/ # 本地 3D 模型资源
fta-visual-system/public/demo/
fta-visual-system/raw-FTA/
```

这些目录不是每个同学都必须有。没有旧数据时，可以从前端上传 PDF，通过 `FTA-KB` 重新构建知识库，再同步到 `FTA-GNR`。

不要手动复制或提交这些目录：

```text
node_modules/
dist/
.venv/
__pycache__/
.idea/
*.iml
```

MongoDB 和 Neo4j 中的数据也不在 Git 里。如果需要复用别人的知识库或故障树结果，需要单独导入数据库，或使用 `FTA-KB/output/` 里的产物重新同步。

## 9. 常见问题

### worktree 添加失败

先检查远程分支：

```powershell
git branch -a
```

确认能看到：

```text
origin/kb-v2
origin/generate-fta
```

### KB 任务成功但同步失败

通常检查这几项：

```text
FTA-GNR 是否已启动
FTA-KB/.env 中 GENERATE_FTA_BASE_URL 是否为 http://127.0.0.1:8000
MongoDB 是否可连接
Neo4j 是否可连接
NEO4J_PASSWORD 是否正确
```

### 新环境没有历史故障树

这是正常的。历史故障树存在 MongoDB，知识图谱存在 Neo4j，不会随着 Git clone 自动出现。需要导入数据库，或者重新跑知识库构建和故障树生成流程。
