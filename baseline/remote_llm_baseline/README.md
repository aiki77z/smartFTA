# 远程 LLM 基线与 LoRA 评估

本目录用于本地调用服务器模型，并比较两种方案在同一测试集上的效果：

- `prompt-only`：Qwen3-8B 原始基座模型，只依赖抽取提示词。
- `LoRA`：Qwen3-8B 基座模型 + SmartFTA LoRA 微调权重。

最终对比统一使用合并后的保留测试集：

```text
D:\smartFTA\testdataset\combined_test_sft_messages.jsonl
```

训练集是：

```text
D:\smartFTA\traindataset\combined_train_sft_messages.jsonl
```

注意：预测时只把 `system + user` 发给模型。原始 `assistant` 答案只作为 `gold` 保留，用于评估，不会发给模型。

## 文件说明

- `run_dataset.py`：本地批量预测脚本，支持 `--workers`、`--resume`、`--limit`、`--start-index`。
- `evaluate_span_relation.py`：主评估脚本，用于评估实体、mention、evidence、关系和逻辑组。
- `evaluate_predictions.py`：较早的简单评估脚本，仅保留作参考。
- `server.py`：较早的 OpenAI-compatible 封装，当前最终流程不使用。
- `baseline/outputs/`：本地预测结果和评估报告目录，已被 Git 忽略。

服务器端脚本放在服务器的 `~/cufan` 目录下：

- `serve_qwen3_gpu.py`：加载 Qwen3-8B 原始基座模型，用于 prompt-only 推理。
- `serve_qwen3_lora_gpu.py`：加载 Qwen3-8B 基座模型和 `qwen3_lora_runs/qwen3_8b_smartfta_lora_v1` LoRA 权重。
- `train_qwen3_lora.py`：LoRA 微调训练脚本。

## 1. 检查服务器环境

在服务器上执行：

```bash
cd ~/cufan
conda activate qwen-ft
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

期望输出中包含：

```text
True
NVIDIA A100 80GB
```

## 2. 启动 Prompt-Only 服务

该服务用于原始基座模型基线。

在服务器上执行：

```bash
cd ~/cufan
conda activate qwen-ft
uvicorn serve_qwen3_gpu:app --host 0.0.0.0 --port 9100
```

在服务器上检查服务：

```bash
curl http://127.0.0.1:9100/health
```

返回结果中不应该包含 `adapter` 字段。

## 3. 启动 LoRA 服务

该服务用于微调后模型。

如果 `9100` 端口上已有 `uvicorn` 服务，先用 `Ctrl+C` 停掉，再启动：

```bash
cd ~/cufan
conda activate qwen-ft
uvicorn serve_qwen3_lora_gpu:app --host 0.0.0.0 --port 9100
```

在服务器上检查服务：

```bash
curl http://127.0.0.1:9100/health
```

返回结果中应该包含：

```json
"adapter": "qwen3_lora_runs/qwen3_8b_smartfta_lora_v1"
```

## 4. 建立 SSH 隧道

在本地 Windows PowerShell 执行：

```powershell
ssh -L 19100:127.0.0.1:9100 yanhan-server
```

保持这个 PowerShell 窗口打开。它建立的映射是：

```text
本地 127.0.0.1:19100 -> 服务器 127.0.0.1:9100
```

再打开另一个本地 PowerShell 窗口，测试：

```powershell
curl http://127.0.0.1:19100/health
```

## 5. 在测试集上运行 Prompt-Only 预测

确认服务器正在运行 `serve_qwen3_gpu.py`，然后在本地执行：

```powershell
cd D:\smartFTA\baseline-remote-llm

python baseline\remote_llm_baseline\run_dataset.py `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --output baseline\outputs\combined_test_prompt_only_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 2
```

如果服务不稳定，可以把 `--workers 2` 改成 `--workers 1`。

注意：如果重跑一个曾经失败过的输出文件，不要直接加 `--resume`，除非你已经删除失败行。`--resume` 会跳过已有 id，包括带错误的行。

## 6. 评估 Prompt-Only 结果

```powershell
python baseline\remote_llm_baseline\evaluate_span_relation.py `
  --input baseline\outputs\combined_test_prompt_only_predictions.jsonl `
  --output baseline\outputs\combined_test_prompt_only_eval_report.json
```

当前有效的 prompt-only 报告满足：

```text
rows = 213
error_rows = 0
```

## 7. 在同一测试集上运行 LoRA 预测

先停掉 prompt-only 服务，启动 `serve_qwen3_lora_gpu.py`。SSH 隧道保持打开，然后在本地执行：

```powershell
cd D:\smartFTA\baseline-remote-llm

python baseline\remote_llm_baseline\run_dataset.py `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --output baseline\outputs\combined_test_lora_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 2
```

## 8. 评估 LoRA 结果

```powershell
python baseline\remote_llm_baseline\evaluate_span_relation.py `
  --input baseline\outputs\combined_test_lora_predictions.jsonl `
  --output baseline\outputs\combined_test_lora_eval_report.json
```

当前有效的 LoRA 报告满足：

```text
rows = 213
error_rows = 0
```

## 9. 当前结果对比

当前报告文件：

```text
baseline\outputs\combined_test_prompt_only_eval_report.json
baseline\outputs\combined_test_lora_eval_report.json
```

当前 F1 对比：

| 模型/参数 | 严格实体 Span+Type F1 | 宽松实体 Span+Type F1 | Normalized Entity F1 | Mention+Type F1 | Evidence Exact F1 | Evidence Text+Type F1 | 关系 Strict Triple F1 | 关系+极性/确定性 F1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Prompt-only | 0.0022 | 0.1445 | 0.4153 | 0.4201 | 0.0022 | 0.3544 | 0.1711 | 0.1517 |
| Prompt-only `Qwen3-14B` | 0.0041 | 0.2613 | 0.4546 | 0.4578 | 0.0041 | 0.4157 | 0.2171 | 0.1806 |
| LoRA `e2_lr1e4_r16_a32_len4096` | 0.0643 | 0.4290 | 0.5434 | 0.5558 | 0.0625 | 0.5689 | 0.3471 | 0.2936 |
| LoRA `e3_lr5e5_r16_a32_len4096` | 0.0594 | 0.3703 | 0.5469 | 0.5633 | 0.0568 | 0.5545 | 0.3408 | 0.2908 |
| LoRA `e2_lr1e4_r32_a64_len4096` | 0.0981 | 0.4439 | 0.5741 | 0.5803 | 0.0980 | 0.5799 | 0.3932 | 0.3313 |
| LoRA `e3_lr1e4_r32_a64_len4096` | 0.1252 | 0.4565 | 0.5903 | 0.6076 | 0.1243 | 0.5962 | 0.3638 | 0.3166 |
| LoRA `e3_lr7e5_r32_a64_len4096` | 0.0963 | 0.4320 | 0.5716 | 0.5847 | 0.0920 | 0.5787 | 0.3851 | 0.3186 |
| LoRA `Qwen3-14B e2_lr8e5_r32_a64_len4096` | 0.1147 | 0.4887 | 0.5653 | 0.5674 | 0.1119 | 0.5677 | 0.3787 | 0.3151 |
| LoRA `Qwen3-14B e25_lr8e5_r32_a64_len4096` | 0.1143 | 0.5107 | 0.5935 | 0.5987 | 0.1125 | 0.5920 | 0.4050 | 0.3614 |
| LoRA `Qwen3-14B e2_lr1e4_r32_a64_len4096` | 0.1150 | 0.4781 | 0.5704 | 0.5763 | 0.1141 | 0.5727 | 0.4024 | 0.3532 |
| LoRA `Qwen3-14B e3_lr8e5_r32_a64_len4096` | **0.1454** | 0.5096 | 0.5853 | 0.5907 | **0.1427** | 0.5817 | 0.4115 | 0.3656 |
| LoRA `Qwen3-14B e35_lr8e5_r32_a64_len4096` | 0.1387 | **0.5305** | **0.6019** | 0.5998 | 0.1387 | 0.5963 | **0.4469** | 0.3952 |
| LoRA `Qwen3-14B e4_lr8e5_r32_a64_len4096` | **0.1456** | 0.4984 | 0.5984 | **0.6091** | **0.1447** | **0.6027** | 0.4431 | **0.4040** |
结论：

- LoRA 明显提升了实体抽取、证据文本抽取和关系抽取。
- 严格 span 和 exact evidence 指标仍然偏低，主要原因是 LLM 不擅长精确输出 `start/end` 偏移。
- 下一步可以让模型只输出 `evidence.text`，再由本地后处理用字符串匹配确定 `start/end`，这样预计能显著提高严格 span/evidence 指标。

语义归一化与语义三元组重评估（`--semantic-normalized --embedding-model D:\models\bge-m3 --semantic-threshold 0.85`）：

| 模型/参数 | Normalized Entity Exact F1 | Hybrid Semantic F1 | All-Entity Semantic F1 | Relation Semantic Triple F1 | Relation Semantic +极性/确定性 F1 |
|---|---:|---:|---:|---:|---:|
| Prompt-only `Qwen3-14B` | 0.4546 | 0.4602 | 0.5404 | 0.2971 | 0.2452 |
| LoRA `e2_lr1e4_r16_a32_len4096` | 0.5434 | 0.5637 | 0.6396 | 0.4227 | 0.3571 |
| LoRA `e3_lr5e5_r16_a32_len4096` | 0.5469 | 0.5646 | 0.6323 | 0.4178 | 0.3559 |
| LoRA `e2_lr1e4_r32_a64_len4096` | 0.5741 | 0.5966 | 0.6694 | 0.4752 | 0.4048 |
| LoRA `e3_lr1e4_r32_a64_len4096` | 0.5903 | 0.6121 | 0.6857 | 0.4476 | 0.3869 |
| LoRA `e3_lr7e5_r32_a64_len4096` | 0.5716 | 0.5832 | 0.6543 | 0.4580 | 0.3814 |
| LoRA `Qwen3-14B e2_lr8e5_r32_a64_len4096` | 0.5653 | 0.5883 | 0.6660 | 0.4816 | 0.4075 |
| LoRA `Qwen3-14B e25_lr8e5_r32_a64_len4096` | 0.5935 | 0.5981 | 0.6733 | 0.4933 | 0.4380 |
| LoRA `Qwen3-14B e2_lr1e4_r32_a64_len4096` | 0.5704 | 0.5830 | 0.6475 | 0.4882 | 0.4289 |
| LoRA `Qwen3-14B e3_lr8e5_r32_a64_len4096` | 0.5853 | 0.6043 | 0.6737 | 0.5059 | 0.4499 |
| LoRA `Qwen3-14B e35_lr8e5_r32_a64_len4096` | **0.6019** | **0.6181** | **0.7000** | **0.5548** | **0.4910** |
| LoRA `Qwen3-14B e4_lr8e5_r32_a64_len4096` | 0.5984 | 0.6133 | 0.6893 | 0.5291 | 0.4815 |

说明：本次语义重评估暂未包含 8B Prompt-only，因为对应预测文件已删除。`Hybrid Semantic F1` 沿用上一版定义：gold 侧可抽取实体（`mention == normalized_name` 或 normalized 出现在 evidence text 中）仍按严格匹配，归纳型实体允许 bge-m3 语义匹配。`All-Entity Semantic F1` 是新增指标：所有实体都允许语义匹配，包括 `mention == normalized_name` 的可抽取实体。语义三元组评估要求 `row_id + relation_type` 严格一致，source 和 target 通过同一语义相似度阈值匹配；带极性/确定性版本额外要求 `polarity + certainty` 严格一致。

## 10. 输出格式

`run_dataset.py` 每行写入一个 JSON 对象：

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

字段含义：

- `prediction`：模型输出。
- `gold`：原始 assistant 标注答案，只用于评估。
- `messages`：实际发送给模型的输入，只包含 `system + user`。
- `error`：请求错误。有效评估要求 `error_rows = 0`。

## 11. 评估指标

主要指标：

- `entity_strict_span_type`：严格匹配 `chunk_id + text_field + start + end + entity_type`。
- `entity_loose_span_type`：span 有重叠且实体类型相同即命中。
- `entity_normalized`：匹配 `entity_type + normalized_name`。
- `entity_mention_type`：匹配 `entity_type + mention`。
- `entity_evidence_exact`：严格匹配 evidence span 和 evidence text。
- `entity_evidence_text_type`：匹配 `entity_type + evidence.text`，忽略 offset。
- `relation_strict_triple`：匹配有向三元组 `source + relation_type + target`。

次要指标：

- `relation_triple_with_polarity_certainty`：关系三元组加 `polarity + certainty`。
- `logic_group`：匹配 `logic_type + members + result`。

每个指标都会报告 `tp`、`fp`、`fn`、`precision`、`recall`、`f1`。

## 12. 微调参数记录

第一轮 LoRA 使用参数：

```text
Base model: /mnt/sda/huggingface/Qwen/Qwen3-8B
Train file: combined_train_sft_messages.jsonl
Eval file: combined_test_sft_messages.jsonl
Output dir: qwen3_lora_runs/qwen3_8b_smartfta_lora_v1
Epochs: 2
Max length: 4096
Learning rate: 1e-4
Batch size: 1
Gradient accumulation steps: 8
LoRA r: 16
LoRA alpha: 32
LoRA dropout: 0.05
GPU: A100 80GB
```

再次训练前，先停止推理服务释放 GPU 显存：

```bash
nvidia-smi
```

如果 `uvicorn` 仍在前台运行，按 `Ctrl+C` 停止。

## 13. 使用 tmux 运行 LoRA 训练任务

正式微调建议放在 `tmux` 中运行，避免 SSH 或 VS Code 断开后训练中断。

### 13.1 新建训练会话

在服务器上执行：

```bash
tmux new -s qwen3-lora
```

进入 tmux 后：

```bash
cd ~/cufan
conda activate qwen-ft
```

如果当前还有推理服务占用 GPU，先停止对应 `uvicorn` 进程，确认显存空闲：

```bash
nvidia-smi
```

### 13.2 Smoke Test

正式训练前可以先跑小样本 smoke test：

```bash
python train_qwen3_lora.py \
  --model-path /mnt/sda/huggingface/Qwen/Qwen3-8B \
  --train-file combined_train_sft_messages.jsonl \
  --eval-file combined_test_sft_messages.jsonl \
  --output-dir qwen3_lora_runs/smoke \
  --train-limit 20 \
  --eval-limit 10 \
  --max-length 2048 \
  --num-train-epochs 1 \
  --per-device-train-batch-size 1 \
  --gradient-accumulation-steps 4 \
  --save-steps 5 \
  --eval-steps 5 \
  --logging-steps 1
```

看到类似下面内容表示流程跑通：

```text
Saved LoRA adapter to qwen3_lora_runs/smoke
```

### 13.3 正式训练命令

第一轮 LoRA 正式训练命令如下：

```bash
python train_qwen3_lora.py \
  --model-path /mnt/sda/huggingface/Qwen/Qwen3-8B \
  --train-file combined_train_sft_messages.jsonl \
  --eval-file combined_test_sft_messages.jsonl \
  --output-dir qwen3_lora_runs/qwen3_8b_smartfta_lora_v1 \
  --max-length 4096 \
  --num-train-epochs 2 \
  --learning-rate 1e-4 \
  --per-device-train-batch-size 1 \
  --gradient-accumulation-steps 8 \
  --lora-r 16 \
  --lora-alpha 32 \
  --save-steps 50 \
  --eval-steps 50 \
  --logging-steps 5
```

训练结束后会看到：

```text
Saved LoRA adapter to qwen3_lora_runs/qwen3_8b_smartfta_lora_v1
```

### 13.4 退出 tmux 但保持训练继续

在 tmux 里按：

```text
Ctrl+b
```

松开后再按：

```text
d
```

这会 detach 当前 tmux 会话，训练会继续在服务器后台运行。

### 13.5 重新进入训练会话

```bash
tmux attach -t qwen3-lora
```

### 13.6 查看现有 tmux 会话

```bash
tmux ls
```

### 13.7 监控 GPU

另开一个服务器终端执行：

```bash
watch -n 2 nvidia-smi
```

### 13.8 停止训练任务

如果训练仍在前台运行，按：

```text
Ctrl+C
```

如果要关闭整个 tmux 会话，可以先进入：

```bash
tmux attach -t qwen3-lora
```

然后执行：

```bash
exit
```

或者在外部直接关闭会话：

```bash
tmux kill-session -t qwen3-lora
```

注意：`tmux kill-session` 会直接终止该会话中的训练进程，只有确认不需要继续训练时再使用。
