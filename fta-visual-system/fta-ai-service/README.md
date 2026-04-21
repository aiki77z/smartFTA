# FTA AI Editor Service

这个服务用于“修改故障树 JSON”的 AI 能力（不是生成新树）。

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
```

启动：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8020 --reload
```

## 前端对接

前端通过 `VITE_FTA_AI_EDITOR_URL` 指向该服务（默认 `http://localhost:8020`），调用：

- `POST /api/fta-edit`

