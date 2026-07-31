# 远程 LLM 基线

本目录包含 prompt-only Qwen 基线的本地运行器和评估脚本。

当前配置：

- Qwen 模型运行在远程服务器 GPU 上。
- 服务器通过 `serve_qwen3_gpu.py` 暴露一个简单的 FastAPI `/extract` 接口。
- 本地机器通过 SSH 隧道连接服务器接口。
- `run_dataset.py` 读取 SFT chat JSONL 文件，并且只把 `system + user` 发送给模型。
- 原始 `assistant` 答案会作为 `gold` 保留下来用于评估，但不会发送给模型。
- `evaluate_span_relation.py` 用于评估实体、证据、mention 和关系指标。

## 文件说明

- `run_dataset.py`：本地批量运行脚本，支持断点续跑和并发 worker。
- `evaluate_span_relation.py`：主评估脚本。
- `evaluate_predictions.py`：较早的简单评估脚本，保留用于对比。
- `server.py`：较早的 OpenAI 兼容封装。当前 Transformers/FastAPI 配置下不使用。
- `requirements.txt`：本地运行和评估所需依赖。

## 1. 启动服务器模型 API

在服务器上执行：

```bash
cd ~/cufan
conda activate qwen-ft
```

确认 GPU 可用：

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

预期结果应包含 `True` 和 `NVIDIA A100 80GB`。

启动 FastAPI 服务：

```bash
uvicorn serve_qwen3_gpu:app --host 0.0.0.0 --port 9100
```

保持该终端窗口打开。如果在 `tmux` 中运行，先按 `Ctrl+b`，再按 `d` 退出会话。

在服务器上检查服务：

```bash
curl http://127.0.0.1:9100/health
```

## 2. 创建 SSH 隧道

在本地 Windows PowerShell 中执行：

```powershell
ssh -L 19100:127.0.0.1:9100 yanhan-server
```

保持该 PowerShell 窗口打开。它会建立如下映射：

```text
local 127.0.0.1:19100 -> server 127.0.0.1:9100
```

再打开一个本地 PowerShell 窗口并测试：

```powershell
curl http://127.0.0.1:19100/health
```

## 3. 运行基线预测

从基线工作区运行：

```powershell
cd D:\smartFTA\baseline-remote-llm
```

`lxz` 示例：

```powershell
python baseline\remote_llm_baseline\run_dataset.py `
  --input ..\dataset\smartFTA\stage1_dataset\lxz_sft_messages.jsonl `
  --output baseline\outputs\lxz_gpu_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 2 `
  --resume
```

`wyf` 示例：

```powershell
python baseline\remote_llm_baseline\run_dataset.py `
  --input ..\dataset\smartFTA\stage1_dataset\wyf_sft_messages.jsonl `
  --output baseline\outputs\wyf_gpu_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 2 `
  --resume
```

`zyt` 示例：

```powershell
python baseline\remote_llm_baseline\run_dataset.py `
  --input ..\dataset\smartFTA\stage1_dataset\zyt_sft_messages.jsonl `
  --output baseline\outputs\zyt_gpu_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 2 `
  --resume
```

如果 Transformers 服务不稳定，使用 `--workers 1`。如果只想做小规模测试，使用 `--limit N`。

## 4. 输出格式

`run_dataset.py` 会写入 JSONL。每一行包含：

```json
{
  "id": 0,
  "prediction": "[ENTITY]\n...",
  "gold": "[ENTITY]\n...",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "error": ""
}
```

注意：

- `messages` 只包含 `system + user`。
- `gold` 来自原始 `assistant` 答案。
- `gold` 只用于评估。

## 5. 运行评估

评估 `lxz`：

```powershell
cd D:\smartFTA\baseline-remote-llm

python baseline\remote_llm_baseline\evaluate_span_relation.py `
  --input baseline\outputs\lxz_gpu_predictions.jsonl `
  --output baseline\outputs\lxz_eval_report.json
```

评估 `wyf`：

```powershell
python baseline\remote_llm_baseline\evaluate_span_relation.py `
  --input baseline\outputs\wyf_gpu_predictions.jsonl `
  --output baseline\outputs\wyf_eval_report.json
```

评估 `zyt`：

```powershell
python baseline\remote_llm_baseline\evaluate_span_relation.py `
  --input baseline\outputs\zyt_gpu_predictions.jsonl `
  --output baseline\outputs\zyt_eval_report.json
```

## 6. 评估指标

主要指标：

- `entity_strict_span_type`：精确匹配 `chunk_id + text_field + start + end + entity_type`。
- `entity_loose_span_type`：与相同实体类型的 span 有重叠。
- `entity_normalized`：匹配 `entity_type + normalized_name`。
- `entity_mention_type`：匹配 `entity_type + mention`。
- `entity_evidence_exact`：精确匹配 evidence span 和 evidence text。
- `entity_evidence_text_type`：匹配 `entity_type + evidence.text`，忽略 offsets。
- `relation_strict_triple`：匹配有向 `source + relation_type + target`。

次要指标：

- `relation_triple_with_polarity_certainty`：关系三元组加上 `polarity + certainty`。
- `logic_group`：`logic_type + members + result`。

每个指标会报告：

- `tp`：真正例
- `fp`：假正例
- `fn`：假负例
- `precision`
- `recall`
- `f1`

## 7. 微调前停止服务

在进行 LoRA 微调之前，停止服务器推理服务以释放 GPU 显存。

如果 `uvicorn` 在前台运行，按：

```text
Ctrl+C
```

如果它运行在 `tmux` 中：

```bash
tmux attach -t qwen3-gpu
```

然后按 `Ctrl+C`。

确认 GPU 显存已释放：

```bash
nvidia-smi
```
