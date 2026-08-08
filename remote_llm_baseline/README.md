# 远程 LLM 基线与 LoRA 评估

本目录用于本地调用服务器模型，并比较 14B 基座、14B LoRA 和两阶段抽取方案在同一测试集上的效果：

- `prompt-only`：Qwen3-14B 原始基座模型，只依赖抽取提示词。
- `LoRA`：Qwen3-14B 基座模型 + SmartFTA LoRA 微调权重。
- `two-stage`：Qwen3-14B 的 entity adapter + relation adapter，当前主线方案。

早期做过 Qwen3-8B 实验，但效果明显弱于 14B，已经作为历史实验舍弃。服务器上即使还保留 8B 模型或旧脚本，后续默认不要再使用。

最终对比统一使用合并后的保留测试集：

```text
D:\smartFTA\testdataset\combined_test_sft_messages.jsonl
```

训练集是：

```text
D:\smartFTA\traindataset\combined_train_sft_messages.jsonl
```

## 文件说明

- `run_dataset.py`：本地批量预测脚本，支持 `--workers`、`--resume`、`--limit`、`--start-index`。
- `evaluate_span_relation.py`：主评估脚本，用于评估实体、mention、evidence、关系和逻辑组。
- `evaluate_predictions.py`：较早的简单评估脚本，仅保留作参考。
- `server.py`：较早的 OpenAI-compatible 封装，当前最终流程不使用。
- `statistic.md`：历史实验指标、对照表和诊断结论。
- `../2stages/`：两阶段数据构造、推理、Oracle 诊断、entity_v2 标注规则与分析脚本。
- `../2stages/entity_v2_normalization_rules.md`：给人、数据清洗脚本和实验复盘看的完整 entity_v2 规范。
- `../2stages/entity_v2_annotation_plan.md`：转交给朋友或 GPT 清洗任务时使用的标注计划。
- `../2stages/entity_v2_prompt_addendum.md`：训练和推理时追加到 entity 阶段 system prompt 的短规则。
- `outputs/`：本地预测结果、评估报告和临时分析结果目录，已被 Git 忽略，交接时不要包含。

当前服务器端主目录是 `~/cufan/qwen3_14b`：

- `serve_qwen3_14b_gpu.py`：加载 Qwen3-14B 原始基座模型，用于 prompt-only 推理。
- `serve_qwen3_14b_lora_gpu.py`：加载 Qwen3-14B 基座模型和指定 LoRA adapter(可以用export导入，也可以在代码文件中直接改adapter path)，用于单阶段、entity 阶段、relation 阶段推理。
- `train_qwen3_14b_lora.py`：Qwen3-14B LoRA 微调训练脚本。
- `qwen3_14b_lora_runs/`：Qwen3-14B adapter 输出目录，当前重点使用 `entity_e4_lr8e5_r32_a64_len4096` 和 `relation_e35_lr8e5_r32_a64_len4096`。

服务器上的 `qwen3_lora_runs/`、`serve_qwen3_gpu.py`、`serve_qwen3_lora_gpu.py`、`train_qwen3_lora.py` 等 8B 旧内容仅保留作历史参考；交接和新实验都以 14B 目录为准。



## 1. 检查服务器环境

在服务器上执行：

```bash
cd ~/cufan/qwen3_14b
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
cd ~/cufan/qwen3_14b
conda activate qwen-ft
MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B \
uvicorn serve_qwen3_14b_gpu:app --host 0.0.0.0 --port 9100
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
cd ~/cufan/qwen3_14b
conda activate qwen-ft
MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B \
ADAPTER_PATH=qwen3_14b_lora_runs/e4_lr8e5_r32_a64_len4096 \
uvicorn serve_qwen3_14b_lora_gpu:app --host 0.0.0.0 --port 9100
```

在服务器上检查服务：

```bash
curl http://127.0.0.1:9100/health
```

返回结果中应该包含：

```json
"adapter": "qwen3_14b_lora_runs/e4_lr8e5_r32_a64_len4096"
```

## 4. 建立 SSH 隧道

在本地 Windows PowerShell 执行：

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -L 19100:127.0.0.1:9100 yanhan-server
```

保持这个 PowerShell 窗口打开。它建立的映射是：

```text
本地 127.0.0.1:19100 -> 服务器 127.0.0.1:9100
```

再打开另一个本地 PowerShell 窗口，测试：

```powershell
curl http://127.0.0.1:19100/health
```

如果出现 `bind [127.0.0.1]:19100: Permission denied`，说明 Windows 本地 `19100` 没有绑定成功，SSH 只是登录到了服务器，并没有建立隧道。换一个本地端口即可，例如：

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -L 19100:127.0.0.1:9100 yanhan-server
curl http://127.0.0.1:19100/health
```

此时后续本地 `--endpoint` 也要同步改成 `http://127.0.0.1:19100/extract`。

## 5. 在测试集上运行 Prompt-Only 预测

确认服务器正在运行 `serve_qwen3_14b_gpu.py`，然后在本地执行：

```powershell
cd D:\smartFTA\baseline-remote-llm

python remote_llm_baseline\run_dataset.py `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --output outputs\combined_test_prompt_only_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 2
```

如果服务不稳定，可以把 `--workers 2` 改成 `--workers 1`。

注意：如果重跑一个曾经失败过的输出文件，不要直接加 `--resume`，除非你已经删除失败行。`--resume` 会跳过已有 id，包括带错误的行。

## 6. 评估 Prompt-Only 结果

```powershell
python remote_llm_baseline\evaluate_span_relation.py `
  --input outputs\combined_test_prompt_only_predictions.jsonl `
  --output outputs\combined_test_prompt_only_eval_report.json
```

当前有效的 prompt-only 报告满足：

```text
rows = 213
error_rows = 0
```

## 7. 在同一测试集上运行 LoRA 预测

先停掉 prompt-only 服务，启动 `serve_qwen3_14b_lora_gpu.py`。SSH 隧道保持打开，然后在本地执行：

```powershell
cd D:\smartFTA\baseline-remote-llm

python remote_llm_baseline\run_dataset.py `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --output outputs\combined_test_lora_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 2
```

## 8. 评估 LoRA 结果

```powershell
python remote_llm_baseline\evaluate_span_relation.py `
  --input outputs\combined_test_lora_predictions.jsonl `
  --output outputs\combined_test_lora_eval_report.json
```

当前有效的 LoRA 报告满足：

```text
rows = 213
error_rows = 0
```

## 9. 结果记录与交接说明

历史实验指标、对照表、Oracle 诊断结论统一记录在：

```text
remote_llm_baseline\statistic.md
```

交接时当前目录下没有包含 `outputs/`。该目录可以自己建立在baseline-remote-llm/目录下，用于保存本地预测、评估报告、临时分析结果。之后做实验的时候，按本文命令跑推理和评估，再把最终指标更新到 `statistic.md`，也不要删除outputs里面的内容，以便核查复现。

交接的核心内容：

```text
remote_llm_baseline\
2stages\
.gitignore
download_bge_m3.py（用于下载语义评估的embedding模型）
```

如果要把 entity_v2 数据清洗任务交给agent，使用：

```text
2stages\entity_v2_normalization_rules.md
2stages\entity_v2_annotation_plan.md
..\traindataset\combined_train_sft_messages.jsonl
```

清洗时应使用原始 SFT 行里的 assistant gold answer，而不是 `outputs/` 里的 entity 预测结果。`outputs/` 只能作为错误分析参考，不作为训练标签来源。

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

8B 第一轮 LoRA 是历史实验，效果不好，当前已经不再作为主线继续训练。后续训练统一使用 Qwen3-14B：

```text
Base model: /mnt/sda/huggingface/Qwen/Qwen3-14B
Train file: combined_train_sft_messages.jsonl
Eval file: combined_test_sft_messages.jsonl
Output dir: qwen3_14b_lora_runs/<experiment_name>
Current entity adapter: qwen3_14b_lora_runs/entity_e4_lr8e5_r32_a64_len4096
Current relation adapter: qwen3_14b_lora_runs/relation_e35_lr8e5_r32_a64_len4096
Recommended epochs: entity 4, relation 3.5
Max length: 4096
Learning rate: 8e-5
Batch size: 1
Gradient accumulation steps: 8
LoRA r: 32
LoRA alpha: 64
LoRA dropout: 0.05
GPU: A100 80GB
```

再次训练前，先停止推理服务，如果 `uvicorn` 仍在前台运行，按 `Ctrl+C` 停止。不要随便关掉服务器上不知道的任务，因为是共用的服务器，可能有别人的任务在跑。

## 13. 使用 tmux 运行 LoRA 训练任务

正式微调建议放在 `tmux` 中运行，避免 SSH 或 VS Code 断开后训练中断。

### 13.1 新建训练会话

在服务器上执行：

```bash
tmux new -s qwen3-lora
```

进入 tmux 后：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft
```

如果当前还有推理服务占用 GPU，先停止对应 `uvicorn` 进程，确认显存空闲：

```bash
nvidia-smi
```

### 13.2 Smoke Test

正式训练前可以先跑小样本 smoke test：

```bash
python train_qwen3_14b_lora.py \
  --model-path /mnt/sda/huggingface/Qwen/Qwen3-14B \
  --train-file combined_train_sft_messages.jsonl \
  --eval-file combined_test_sft_messages.jsonl \
  --output-dir qwen3_14b_lora_runs/smoke \
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
Saved LoRA adapter to qwen3_14b_lora_runs/smoke
```

### 13.3 正式训练命令

当前不建议继续训练旧 8B。14B 单阶段 LoRA 的参考训练命令如下；两阶段 entity/relation 的正式命令见后面的两阶段章节：

```bash
python train_qwen3_14b_lora.py \
  --model-path /mnt/sda/huggingface/Qwen/Qwen3-14B \
  --train-file combined_train_sft_messages.jsonl \
  --eval-file combined_test_sft_messages.jsonl \
  --output-dir qwen3_14b_lora_runs/e4_lr8e5_r32_a64_len4096 \
  --max-length 4096 \
  --num-train-epochs 4 \
  --learning-rate 8e-5 \
  --per-device-train-batch-size 1 \
  --gradient-accumulation-steps 8 \
  --lora-r 32 \
  --lora-alpha 64 \
  --save-steps 50 \
  --eval-steps 50 \
  --logging-steps 5
```

训练结束后会看到：

```text
Saved LoRA adapter to qwen3_14b_lora_runs/e4_lr8e5_r32_a64_len4096
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

## 14. 当前扁平目录结构

`baseline` 夹层已经移除，当前目录约定如下：

```text
baseline-remote-llm/
  remote_llm_baseline/        # 通用预测、评估、旧服务封装脚本
  outputs/                    # 预测结果和评估报告
  2stages/                    # 两阶段数据构造与推理脚本
  download_bge_m3.py
```

因此旧命令里的：

```text
baseline\remote_llm_baseline\run_dataset.py
baseline\outputs\xxx.jsonl
```

现在对应为：

```text
remote_llm_baseline\run_dataset.py
outputs\xxx.jsonl
```

## 15. 两阶段 Entity -> Relation 流程

两阶段目标是先抽实体，再把实体候选作为闭集传给关系模型，约束 `source/target` 必须来自实体候选的 `normalized_name`。

### 15.1 本地生成两阶段训练集

在本地 PowerShell 执行：

```powershell
cd D:\smartFTA\baseline-remote-llm

python 2stages\build_two_stage_datasets.py `
  --train-input ..\traindataset\combined_train_sft_messages.jsonl `
  --test-input ..\testdataset\combined_test_sft_messages.jsonl `
  --train-entity-output ..\traindataset\combined_train_entity_sft_messages.jsonl `
  --train-relation-output ..\traindataset\combined_train_relation_sft_messages.jsonl `
  --test-entity-output ..\testdataset\combined_test_entity_sft_messages.jsonl `
  --test-relation-output ..\testdataset\combined_test_relation_sft_messages.jsonl
```

### 15.2 服务器训练两个 LoRA adapter

把新生成的四个 `*_entity_*` / `*_relation_*` 数据文件同步到服务器 `~/cufan/` 后，分别训练实体 adapter 和关系 adapter。

实体 adapter：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft

python train_qwen3_14b_lora.py \
  --model-path /mnt/sda/huggingface/Qwen/Qwen3-14B \
  --train-file ../combined_train_entity_sft_messages.jsonl \
  --eval-file ../combined_test_entity_sft_messages.jsonl \
  --output-dir qwen3_14b_lora_runs/entity_e4_lr8e5_r32_a64_len4096 \
  --max-length 4096 \
  --num-train-epochs 4 \
  --learning-rate 8e-5 \
  --per-device-train-batch-size 1 \
  --per-device-eval-batch-size 1 \
  --gradient-accumulation-steps 8 \
  --lora-r 32 \
  --lora-alpha 64 \
  --lora-dropout 0.05 \
  --save-steps 25 \
  --eval-steps 25 \
  --logging-steps 5
```

关系 adapter：

```bash
python train_qwen3_14b_lora.py \
  --model-path /mnt/sda/huggingface/Qwen/Qwen3-14B \
  --train-file ../combined_train_relation_sft_messages.jsonl \
  --eval-file ../combined_test_relation_sft_messages.jsonl \
  --output-dir qwen3_14b_lora_runs/relation_e35_lr8e5_r32_a64_len4096 \
  --max-length 4096 \
  --num-train-epochs 3.5 \
  --learning-rate 8e-5 \
  --per-device-train-batch-size 1 \
  --per-device-eval-batch-size 1 \
  --gradient-accumulation-steps 8 \
  --lora-r 32 \
  --lora-alpha 64 \
  --lora-dropout 0.05 \
  --save-steps 25 \
  --eval-steps 25 \
  --logging-steps 5
```

### 15.3 本地跑 Entity 阶段

服务器先启动实体 adapter：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft

MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B \
ADAPTER_PATH=qwen3_14b_lora_runs/entity_e4_lr8e5_r32_a64_len4096 \
uvicorn serve_qwen3_14b_lora_gpu:app --host 0.0.0.0 --port 9100
```

本地保持 SSH 隧道：

```powershell
ssh -L 19100:127.0.0.1:9100 yanhan-server
```

另开 PowerShell：

```powershell
cd D:\smartFTA\baseline-remote-llm

python 2stages\run_two_stage_dataset.py `
  --stage entity `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_twostage_relation_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_qwen3_14b_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 1 `
  --resume
```

### 15.4 本地跑 Relation 阶段

服务器停止实体服务，改启关系 adapter：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft

MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B \
ADAPTER_PATH=qwen3_14b_lora_runs/relation_e35_lr8e5_r32_a64_len4096 \
uvicorn serve_qwen3_14b_lora_gpu:app --host 0.0.0.0 --port 9100
```

本地执行：

```powershell
cd D:\smartFTA\baseline-remote-llm

python 2stages\run_two_stage_dataset.py `
  --stage relation `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_twostage_relation_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_qwen3_14b_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 1 `
  --resume
```

### 15.5 拼接并评估

```powershell
python 2stages\run_two_stage_dataset.py `
  --stage stitch `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_twostage_relation_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_qwen3_14b_predictions.jsonl

python remote_llm_baseline\evaluate_span_relation.py `
  --input outputs\combined_test_twostage_qwen3_14b_predictions.jsonl `
  --output outputs\combined_test_twostage_qwen3_14b_eval_report.json
```

### 15.5 Oracle Entity Relation 诊断

这个实验用于判断关系阶段的上限：关系模型仍然正常推理，但 `[ENTITY_CANDIDATES]` 不再来自 entity 模型预测，而是直接从测试集 gold `[ENTITY]` 段抽取。若 oracle relation 指标明显高于普通 two-stage，说明主要瓶颈在 entity；若提升不明显，说明 relation 训练/提示本身还需要改。

现在跑过prompt-only的oracle和一次微调过后的oracle，具体数据都在表格里记录了。

服务器端只需要启动 relation adapter：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft

MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B \
ADAPTER_PATH=qwen3_14b_lora_runs/relation_e35_lr8e5_r32_a64_len4096 \
uvicorn serve_qwen3_14b_lora_gpu:app --host 0.0.0.0 --port 9100
```

本地保持 SSH tunnel 后，运行 oracle relation：

```powershell
cd D:\smartFTA\baseline-remote-llm

python 2stages\run_two_stage_dataset.py `
  --stage relation `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_oracle_entity_relation_qwen3_14b.jsonl `
  --final-output outputs\combined_test_oracle_entity_relation_qwen3_14b_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 1 `
  --oracle-entity-candidates
```

拼接 oracle 最终预测并评估：

```powershell
python 2stages\run_two_stage_dataset.py `
  --stage stitch `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_oracle_entity_relation_qwen3_14b.jsonl `
  --final-output outputs\combined_test_oracle_entity_relation_qwen3_14b_predictions.jsonl `
  --oracle-entity-candidates

python remote_llm_baseline\evaluate_span_relation.py `
  --input outputs\combined_test_oracle_entity_relation_qwen3_14b_predictions.jsonl `
  --output outputs\combined_test_oracle_entity_relation_qwen3_14b_eval_report.json

python remote_llm_baseline\evaluate_span_relation.py `
  --input outputs\combined_test_oracle_entity_relation_qwen3_14b_predictions.jsonl `
  --output outputs\combined_test_oracle_entity_relation_qwen3_14b_semantic_eval_report.json `
  --semantic-normalized `
  --embedding-model D:\models\bge-m3 `
  --semantic-threshold 0.85 `
  --embedding-batch-size 16
```

### 15.6 Entity Error Analysis

Oracle 结果说明 relation 阶段上限较高，因此下一步重点分析 entity 候选。运行：

```powershell
cd D:\smartFTA\baseline-remote-llm

python 2stages\analyze_entity_errors.py `
  --entity-output outputs\combined_test_twostage_entity_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_qwen3_14b_predictions.jsonl `
  --out-json outputs\combined_test_twostage_entity_error_analysis_qwen3_14b.json `
  --out-md outputs\combined_test_twostage_entity_error_analysis_qwen3_14b.md `
  --top-k 80
```

当前诊断结果：

- `entity_normalized_f1 = 0.5971`
- `entity_mention_type_f1 = 0.6064`
- `gold_relations = 625`
- `relation_endpoint_possible = 315`
- `relation_endpoint_blocked = 310`
- `relation_endpoint_possible_recall_ceiling = 0.5040`

这表示在普通 two-stage 下，约一半 gold relation 的 source/target 无法同时被当前 entity 候选覆盖。后续应优先做 `entity_v2` 数据规范和重训，再复用当前 `relation_e35` 验证端到端提升。

### 15.7 Entity V2 标注、训练与验证

`entity_v2_normalization_rules.md` 是给人、数据清洗脚本、prompt 维护者和实验复盘看的完整规范；模型训练时不直接依赖这份长文档。训练和推理实际追加的是短版 `entity_v2_prompt_addendum.md`。

当前已生成的数据是 prompt-addendum 版：assistant gold `[ENTITY]` 基本仍来自原始训练集，只是在 system prompt 中追加了短规则。它可用于快速验证“规则提示是否有帮助”，但不是最终的 label-cleaned entity_v2 数据。

```text
2stages\data\entity_v2\combined_train_entity_v2_sft_messages.jsonl
2stages\data\entity_v2\combined_test_entity_v2_sft_messages.jsonl
2stages\data\entity_v2\combined_train_relation_v2_sft_messages.jsonl
2stages\data\entity_v2\combined_test_relation_v2_sft_messages.jsonl
```

如需重新生成 prompt-addendum 版：

```powershell
cd D:\smartFTA\baseline-remote-llm

python 2stages\build_two_stage_datasets.py `
  --train-input ..\traindataset\combined_train_sft_messages.jsonl `
  --test-input ..\testdataset\combined_test_sft_messages.jsonl `
  --train-entity-output 2stages\data\entity_v2\combined_train_entity_v2_sft_messages.jsonl `
  --train-relation-output 2stages\data\entity_v2\combined_train_relation_v2_sft_messages.jsonl `
  --test-entity-output 2stages\data\entity_v2\combined_test_entity_v2_sft_messages.jsonl `
  --test-relation-output 2stages\data\entity_v2\combined_test_relation_v2_sft_messages.jsonl `
  --entity-rules-file 2stages\entity_v2_prompt_addendum.md
```

若要做真正的 label-cleaned entity_v2，应先按 `2stages\entity_v2_annotation_plan.md` 清洗原始 SFT 的 assistant `[ENTITY]` 标签，再生成类似下面的文件：

```text
2stages\data\entity_v2_clean\combined_train_entity_v2_clean_sft_messages.jsonl
2stages\data\entity_v2_clean\combined_test_entity_v2_clean_sft_messages.jsonl
```

训练优先使用 label-cleaned 版；如果清洗尚未完成，再使用 prompt-addendum 版做临时实验。

把 entity_v2 数据同步到服务器：

```powershell
cd D:\smartFTA\baseline-remote-llm

scp .\2stages\data\entity_v2\combined_train_entity_v2_sft_messages.jsonl yanhan@worker1:~/cufan/
scp .\2stages\data\entity_v2\combined_test_entity_v2_sft_messages.jsonl yanhan@worker1:~/cufan/
```

服务器训练 entity_v2 adapter：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft

python train_qwen3_14b_lora.py \
  --model-path /mnt/sda/huggingface/Qwen/Qwen3-14B \
  --train-file ../combined_train_entity_v2_sft_messages.jsonl \
  --eval-file ../combined_test_entity_v2_sft_messages.jsonl \
  --output-dir qwen3_14b_lora_runs/entity_v2_e4_lr8e5_r32_a64_len4096 \
  --max-length 4096 \
  --num-train-epochs 4 \
  --learning-rate 8e-5 \
  --per-device-train-batch-size 1 \
  --per-device-eval-batch-size 1 \
  --gradient-accumulation-steps 8 \
  --lora-r 32 \
  --lora-alpha 64 \
  --lora-dropout 0.05 \
  --save-steps 25 \
  --eval-steps 25 \
  --logging-steps 5
```

训练完成后，先只替换 entity adapter，relation 仍复用 `relation_e35`。

启动 entity_v2 服务：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft

MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B \
ADAPTER_PATH=qwen3_14b_lora_runs/entity_v2_e4_lr8e5_r32_a64_len4096 \
uvicorn serve_qwen3_14b_lora_gpu:app --host 0.0.0.0 --port 9100
```

本地跑 entity_v2：

```powershell
cd D:\smartFTA\baseline-remote-llm

python 2stages\run_two_stage_dataset.py `
  --stage entity `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_v2_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 1 `
  --entity-rules-file 2stages\entity_v2_prompt_addendum.md
```

然后服务器切换到 relation_e35：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft

MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B \
ADAPTER_PATH=qwen3_14b_lora_runs/relation_e35_lr8e5_r32_a64_len4096 \
uvicorn serve_qwen3_14b_lora_gpu:app --host 0.0.0.0 --port 9100
```

本地跑 relation、stitch、评估：

```powershell
python 2stages\run_two_stage_dataset.py `
  --stage relation `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_v2_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl `
  --endpoint http://127.0.0.1:19100/extract `
  --max-tokens 2048 `
  --workers 1

python 2stages\run_two_stage_dataset.py `
  --stage stitch `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_v2_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl

python remote_llm_baseline\evaluate_span_relation.py `
  --input outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl `
  --output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_eval_report.json

python 2stages\analyze_entity_errors.py `
  --entity-output outputs\combined_test_twostage_entity_v2_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl `
  --out-json outputs\combined_test_twostage_entity_v2_error_analysis_qwen3_14b.json `
  --out-md outputs\combined_test_twostage_entity_v2_error_analysis_qwen3_14b.md `
  --top-k 80
```

判断是否成功主要看两个数：

- `relation_endpoint_possible` 是否从 `315 / 625` 明显提升。
- 端到端 `relation_strict_triple F1` 是否超过 `0.4469`。
