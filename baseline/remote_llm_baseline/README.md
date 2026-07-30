# Remote LLM baseline

这套脚本用于跑“纯大模型抽取实体和关系”的 dataset baseline：

- 模型在服务器上运行。
- 服务器启动一个 HTTP 接口。
- 本地读取 `dataset/smartFTA/stage1_dataset/*.jsonl`，把 `system + user` 发给服务器。
- 服务器返回 `[ENTITY] / [RELATION] / [LOGIC_GROUP]` 文本。

## 1. 服务器启动模型服务

推荐先用 vLLM / Xinference / Ollama 暴露一个 OpenAI-compatible chat 接口，例如 vLLM：

```bash
python -m vllm.entrypoints.openai.api_server \
  --model /path/to/your/model \
  --host 127.0.0.1 \
  --port 8000
```

只监听 `127.0.0.1` 即可，下面的 `server.py` 会对外提供项目接口。

## 2. 服务器启动抽取接口

```bash
cd /path/to/smartFTA
pip install -r baseline/remote_llm_baseline/requirements.txt

export LLM_MODEL=/path/to/your/model-or-model-name
export LLM_API_BASE=http://127.0.0.1:8000/v1
export LLM_API_KEY=EMPTY
export BASELINE_TOKEN=change-me

uvicorn baseline.remote_llm_baseline.server:app --host 0.0.0.0 --port 9000
```

如果服务器防火墙只允许内网访问，把 `9000` 端口开放给本地机器即可。

## 3. 本地批量跑 dataset

```bash
cd D:\smartFTA
python baseline\remote_llm_baseline\run_dataset.py ^
  --input dataset\smartFTA\stage1_dataset\lxz_sft_messages.jsonl ^
  --output baseline\outputs\lxz_remote_predictions.jsonl ^
  --endpoint http://SERVER_IP:9000/extract ^
  --token change-me
```

输出 JSONL 每行包含：

- `id`: 样本序号
- `prediction`: 模型输出
- `gold`: 原标注答案
- `messages`: 实际发送给模型的 `system/user`

## API

`POST /extract`

```json
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "temperature": 0.0,
  "max_tokens": 2048
}
```

返回：

```json
{
  "text": "[ENTITY]\n...",
  "model": "...",
  "usage": {}
}
```

