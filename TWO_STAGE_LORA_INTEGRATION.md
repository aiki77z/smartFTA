# FTA-KB 微调模型两阶段接入与使用说明

本文档说明如何把远程服务器上的 Qwen3-14B 两阶段 LoRA 模型作为**可选开关**接入 FTA-KB，并在不删除原有单阶段 DeepSeek 逻辑的前提下使用。

---

## 1. 模型与产物

基座模型：

```text
/mnt/sda/huggingface/Qwen/Qwen3-14B
```

最佳两阶段 adapter：

```text
实体阶段：qwen3_14b_lora_runs/entity_v2_clean_e4_lr8e5_r32_a64_len4096
关系阶段：qwen3_14b_lora_runs/relation_e35_lr8e5_r32_a64_len4096
```

两阶段含义：

1. entity adapter 只输出 `[ENTITY]`。
2. relation adapter 接收原始文本和 `[ENTITY_CANDIDATES]`，只输出 `[RELATION]` 和 `[LOGIC_GROUP]`。

---

## 2. 总体架构

```text
FTA-KB
  └─ OpenAI SDK 调用 /v1/chat/completions
          │
          ▼
本地 SSH 隧道 29100
          │
          ▼
服务器 9200：serve_two_stage_proxy.py
          ├─ model=qwen3-entity   → 9100 entity adapter
          └─ model=qwen3-relation → 9101 relation adapter
```

本地流水线：

```text
PDF
  → MinerU 转 Markdown
  → Markdown 分块
  → MongoDB 导入 chunks
  → 两阶段 LLM 抽取
  → 实体聚类 / 关系聚合
  → 可选写入 Neo4j
```

---

## 3. FTA-KB 本地代码改动清单

新增文件：

```text
FTA-KB/llm_ft_extractor.py
FTA-KB/entity_v2_prompt_addendum.md
```

修改文件：

```text
FTA-KB/llm_annotation_extractor.py
```

改动方式：仅在 `generate_annotation_dataset()` 开头增加以下分支：

```python
if os.getenv("LLM_MODE", "single") == "two-stage":
    from llm_ft_extractor import generate_annotation_dataset_two_stage

    return generate_annotation_dataset_two_stage(
        chunks=chunks,
        output_csv=output_csv,
        raw_jsonl=raw_jsonl,
        base_url=os.getenv("LLM_FT_BASE_URL", base_url),
        api_key=os.getenv("LLM_FT_API_KEY", api_key),
        entity_model=os.getenv("LLM_FT_ENTITY_MODEL", ""),
        relation_model=os.getenv("LLM_FT_RELATION_MODEL", ""),
        entity_rules_file=os.getenv("LLM_FT_ENTITY_RULES_FILE", ""),
        temperature=temperature,
        max_tokens=max_tokens,
        workers=workers,
        dry_run=dry_run,
        debug_context=debug_context,
        sleep_seconds=sleep_seconds,
        continue_on_error=continue_on_error,
    )
```

原有单阶段逻辑全部保留。`LLM_MODE=single` 时行为不变。

---

## 4. FTA-KB 环境准备

在 Windows PowerShell 中：

```powershell
cd D:\classes\waibao\FTA-NEW\smartFTA\FTA-KB

python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
```

PDF 转 Markdown 需要 MinerU：

```powershell
python -m pip install uv
uv pip install -U "mineru[all]" -i https://mirrors.aliyun.com/pypi/simple
```

安装后验证：

```powershell
mineru --version
```

MongoDB 需要在本机运行：

```powershell
Get-Service MongoDB
Test-NetConnection -ComputerName 127.0.0.1 -Port 27017
```

---

## 5. FTA-KB `.env` 配置

文件位置：

```text
D:\classes\waibao\FTA-NEW\smartFTA\FTA-KB\.env
```

参考配置：

```env
# 切换开关
LLM_MODE=two-stage

# 微调模型两阶段服务
LLM_FT_BASE_URL=http://127.0.0.1:29100/v1
LLM_FT_API_KEY=EMPTY
LLM_FT_ENTITY_MODEL=qwen3-entity
LLM_FT_RELATION_MODEL=qwen3-relation
LLM_FT_ENTITY_RULES_FILE=entity_v2_prompt_addendum.md

# 原单阶段 DeepSeek，保留用于回退
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# MongoDB
MONGO_URI=mongodb://127.0.0.1:27017/
MONGO_DB_NAME=fault-tree-trial

# Neo4j，完整入库时再配置
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=
NEO4J_DATABASE=neo4j
```

切换回单阶段：

```env
LLM_MODE=single
```

---

## 6. 服务器侧模型服务

### 6.1 启动 entity adapter

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft
export MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B
export ADAPTER_PATH=qwen3_14b_lora_runs/entity_v2_clean_e4_lr8e5_r32_a64_len4096
uvicorn serve_qwen3_14b_lora_gpu:app --host 0.0.0.0 --port 9100
```

### 6.2 启动 relation adapter

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft
export MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B
export ADAPTER_PATH=qwen3_14b_lora_runs/relation_e35_lr8e5_r32_a64_len4096
uvicorn serve_qwen3_14b_lora_gpu:app --host 0.0.0.0 --port 9101
```

### 6.3 启动 OpenAI 兼容代理

服务器新增 `serve_two_stage_proxy.py`，内容如下：

```python
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, List

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


ENTITY_EXTRACT_URL = os.getenv("ENTITY_EXTRACT_URL", "http://127.0.0.1:9100/extract")
RELATION_EXTRACT_URL = os.getenv("RELATION_EXTRACT_URL", "http://127.0.0.1:9101/extract")

ENTITY_MODELS = {"qwen3-entity"}
RELATION_MODELS = {"qwen3-relation"}


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage] = Field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int = 2048


app = FastAPI(title="Qwen3 LoRA two-stage proxy")


def _extract_url(model: str) -> str:
    if model in ENTITY_MODELS:
        return ENTITY_EXTRACT_URL
    if model in RELATION_MODELS:
        return RELATION_EXTRACT_URL
    raise HTTPException(status_code=404, detail=f"unknown model: {model}")


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "entity_url": ENTITY_EXTRACT_URL,
        "relation_url": RELATION_EXTRACT_URL,
        "entity_models": sorted(ENTITY_MODELS),
        "relation_models": sorted(RELATION_MODELS),
    }


@app.post("/v1/chat/completions")
def chat_completions(payload: ChatCompletionRequest) -> Dict[str, Any]:
    url = _extract_url(payload.model)

    upstream_body = {
        "messages": [
            {"role": message.role, "content": message.content}
            for message in payload.messages
        ],
        "temperature": payload.temperature,
        "max_tokens": payload.max_tokens,
    }

    try:
        response = requests.post(url, json=upstream_body, timeout=900)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"upstream request failed: {exc}") from exc

    data = response.json()
    content = str(data.get("text", "") or "").strip()
    usage = data.get("usage") or {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }
```

启动代理：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft
export ENTITY_EXTRACT_URL=http://127.0.0.1:9100/extract
export RELATION_EXTRACT_URL=http://127.0.0.1:9101/extract
uvicorn serve_two_stage_proxy:app --host 0.0.0.0 --port 9200
```

建议三个服务都用 `tmux` 启动：

```bash
tmux new -s entity-service
tmux new -s relation-service
tmux new -s ft-proxy
```

退出 tmux：

```text
Ctrl+b
d
```

重新进入：

```bash
tmux attach -t entity-service
tmux attach -t relation-service
tmux attach -t ft-proxy
```

验证：

```bash
curl http://127.0.0.1:9100/health
curl http://127.0.0.1:9101/health
curl http://127.0.0.1:9200/health
```

---

## 7. 本地 SSH 隧道

在本地 Windows 另开一个 PowerShell：

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -L 29100:127.0.0.1:9200 yanhan-server
```

该窗口保持打开。映射关系：

```text
本地 127.0.0.1:29100 → 服务器 127.0.0.1:9200
```

---

## 8. 运行 FTA-KB 两阶段测试

```powershell
cd D:\classes\waibao\FTA-NEW\smartFTA\FTA-KB
.\.venv\Scripts\Activate.ps1

python kb_pipeline_v2.py `
  --env-file .env `
  --input-file test.pdf `
  --output-dir ./output_pdf_test `
  --chunk-size 800 `
  --skip-clean `
  --embedding-backend none `
  --skip-cross-file-import
```

如果输入已经是 Markdown，可再加：

```powershell
--skip-mineru
```

注意：

- PDF 测试时不要加 `--skip-mineru`，否则无法完成 OCR。
- 重复使用同一个 `--output-dir` 和版本号时，旧产物目录必须为空；否则 MinerU 会报 `version directory already exists and is not empty`。
- 第一次测试可以用 `--embedding-backend none --skip-cross-file-import`，跳过 embedding 和 Neo4j。

---

## 9. 成功标准

运行日志最后应出现：

```text
KB_STAGE=success|KB V2 pipeline finished
"status": "success"
```

LLM 抽取结果：

```json
{
  "llm_extraction": {
    "samples": 26,
    "failed": 0
  }
}
```

产物目录：

```text
output_pdf_test\test\test_v1\
```

重点文件：

```text
test_chunks.json
test.csv
test_raw_llm.jsonl
entity_clustering\cluster_intermediate.json
```

其中 `test_raw_llm.jsonl` 应包含：

```text
entity_prompt
relation_prompt
entity_llm_output
llm_output
```

---

## 10. 回退到单阶段 DeepSeek

编辑 `.env`：

```env
LLM_MODE=single
```

并确保：

```env
LLM_API_KEY=你的 DeepSeek key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
```

重新运行流水线即可回到原有单阶段逻辑。

---

## 11. 常见问题

### 11.1 MinerU 报 WinError 1314

原因：Windows 未开启开发者模式，无法创建符号链接。

解决：

```text
设置 → 隐私和安全性 → 开发者选项 → 开发人员模式 → 打开
```

或者用管理员 PowerShell 重新运行。

### 11.2 LLM 抽取返回 502

检查服务器三个服务：

```bash
tmux capture-pane -t ft-proxy -p | tail -80
tmux capture-pane -t entity-service -p | tail -80
tmux capture-pane -t relation-service -p | tail -80
```

确认：

```bash
curl http://127.0.0.1:9100/health
curl http://127.0.0.1:9101/health
curl http://127.0.0.1:9200/health
```

重点看代理日志中的 `upstream request failed: ...`。

### 11.3 提示 `version directory already exists and is not empty`

原因：上一次运行留下了旧产物目录。

解决：

```powershell
Remove-Item -Recurse -Force D:\classes\waibao\FTA-NEW\smartFTA\FTA-KB\output_pdf_test\test
```

或换一个输出目录：

```powershell
--output-dir ./output_pdf_test2
```

### 11.4 `context=False` 是否正常

正常。`context=False` 表示当前 chunk 未使用上一个 chunk 作为上下文，常见原因包括：

```text
no_previous_chunk
different_chapter
non_continuous_chunk_id
different_file_id
```

只要没有 `FAILED`，并且每行都有 `entities/relations/logic_groups` 统计，即为正常。

