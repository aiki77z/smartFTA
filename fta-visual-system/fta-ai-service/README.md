# FTA AI Assistant Service

这个服务是可视化系统中的 AI 助手服务。旧接口仍支持“修改故障树 JSON”，新增的 AssistantAgent 负责会话记忆、上下文管理、意图识别，并通过 HTTP 工具调度 FTA-GNR 的生成/校验接口。

## 运行

在 `fta-visual-system/fta-ai-service` 目录下：

建议使用 **Python 3.11**（你的项目其它后端也在用 3.11；部分依赖在 3.13 上可能缺少可用的二进制轮子）。

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

创建 `.env`（你会提供 key/地址/模型）：

```env
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
OPENAI_MODEL=...

# 可选：FTA-GNR 服务地址，默认 http://localhost:8000
FTA_GNR_BASE_URL=http://localhost:8000

# 可选：未来 FTA-GNR 做成多智能体后，可把 AssistantAgent 的生成调度切到该路径
# 例如 /api/agent/run；为空时继续调用 /api/tree/generate
FTA_GNR_AGENT_RUN_PATH=

# 可选：AssistantAgent 本地记忆目录，默认 app/data
FTA_AI_SERVICE_DATA_DIR=
```

启动：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8020 --reload
```

## 前端对接

前端通过 `VITE_FTA_AI_EDITOR_URL` 指向该服务（默认 `http://localhost:8020`），调用：

- `POST /api/fta-edit`：旧版单次故障树 JSON 编辑接口，当前前端可继续使用。
- `POST /api/assistant/message`：新版 AssistantAgent 统一入口。
- `GET /api/assistant/session/{session_id}`：读取会话记忆、消息和待确认动作。

AssistantAgent 不复制 FTA-GNR 的图谱检索、证据召回和故障树生成逻辑，只负责交互层智能调度。当前它会调用 FTA-GNR 的 `/api/tree/generate` 和 `/api/tree/validate`；后续 FTA-GNR 内部升级为草稿树生成智能体链后，可通过 `FTA_GNR_AGENT_RUN_PATH` 切换到新的 agent-run 接口。

