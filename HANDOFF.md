# SmartFTA 远程 LLM 抽取实验交接说明

## 1. 项目背景

这个分支负责 SmartFTA 数据的 LLM 信息抽取实验。目标是从故障文本中抽取三类结构化结果：

- `[ENTITY]`：故障事件、维修方法、触发规则、报警码、故障类别等实体。
- `[RELATION]`：实体之间的因果、包含、触发、处置等关系。
- `[LOGIC_GROUP]`：逻辑组合关系。

本地主要负责编排数据、调用远程模型服务、保存预测结果和评估指标；服务器负责加载 Qwen3-14B 或 LoRA adapter 做推理/训练。

当前主线模型是：

```text
Qwen3-14B
```

早期做过 Qwen3-8B，但效果不好，已经舍弃。后续不要再继续 8B 实验。

## 2. 当前分支结构

当前重点目录如下：

```text
baseline-remote-llm/
  remote_llm_baseline/       # 通用预测、评估脚本和 README
  2stages/                   # 两阶段数据构造、推理、分析、entity_v2 规则
  download_bge_m3.py         # 下载语义评估用 bge-m3
  HANDOFF.md                 # 本交接说明
```

不要交接或提交：

```text
outputs/
```

`outputs/` 只放本地预测结果、评估报告和临时分析文件，体积大，而且可以重新生成。

如果远端分支里还看到 `fta-visual-system/`，那是不应该保留在这个分支里的旧前端目录，需要删除：

```powershell
cd D:\smartFTA\baseline-remote-llm
git rm -r --sparse fta-visual-system
git commit -m "Remove visual system from remote LLM baseline branch"
git push origin baseline-remote-llm
```

## 3. 已经做过什么：一次性抽取

最早的方案是“一次性抽取”：给模型输入一条文本，让模型一次性输出 `[ENTITY]`、`[RELATION]`、`[LOGIC_GROUP]` 三部分。

做过的对照包括：

- Qwen3-14B prompt-only。
- Qwen3-14B 单阶段 LoRA。
- 多组 epoch、learning rate、LoRA rank 的参数实验。

结果记录在：

```text
remote_llm_baseline/statistic.md
```

主要结论：

- LoRA 明显优于 prompt-only。
- Qwen3-14B 明显优于早期 8B。
- 单阶段最好的 relation strict F1 大约在 `0.44` 左右。
- 严格 span 和 evidence offset 指标很低，主要因为 LLM 不擅长精确计算 `start/end` 偏移。

所以后续暂时不要把精力放在 offset 上。可以先用 `evidence.text` 和后处理字符串匹配解决。

## 4. 为什么转向两阶段抽取

一次性抽取的问题是：模型在生成 relation 时会自由生成 source/target，实体名称很容易不稳定。

典型错误是：

```text
gold entity: 裂纹
model entity: 裂纹大于25mm
```

语义接近，但 strict triple 评估会判错，因为 relation 的 source/target 必须和 entity 的 `normalized_name` 对齐。

所以改成两阶段：

```text
阶段 A：只抽 ENTITY
阶段 B：输入原文 + 阶段 A 的实体候选列表，只抽 RELATION
```

relation 阶段强制 source/target 只能来自 entity 候选列表。这样可以减少 relation 自由生成实体名导致的错误。

## 5. 当前两阶段结果和瓶颈

当前两阶段组合：

```text
entity adapter:   qwen3_14b_lora_runs/entity_e4_lr8e5_r32_a64_len4096
relation adapter: qwen3_14b_lora_runs/relation_e35_lr8e5_r32_a64_len4096
```

当前两阶段端到端结果：

```text
relation strict F1: 0.4419
relation semantic F1: 0.5368
```

做过 Oracle Entity Relation 诊断：关系阶段直接使用 gold entity 作为候选。

Oracle 结果：

```text
relation strict F1: 0.8085
relation semantic F1: 0.8150
```

这个差距说明：relation adapter 本身已经比较强，当前真正瓶颈是 entity 候选质量。

关键诊断：

```text
gold_relations = 625
普通 two-stage entity 能覆盖两端点的 gold relation = 315
被 entity 候选阻断的 gold relation = 310
relation recall 粗略上限约为 0.5040
```

所以接下来优先做：

```text
entity_v2 数据清洗 + 重新训练 entity adapter
```

暂时不优先继续微调 relation。

## 6. 接下来要做什么

### 6.1 做 entity_v2 标注清洗

目标不是让标签更好看，而是提高 relation endpoint coverage。

最低目标：

```text
relation_endpoint_possible: 315 / 625 -> 400 / 625
```

理想目标：

```text
relation_endpoint_possible: 315 / 625 -> 480 / 625
```

先读：

```text
2stages/entity_v2_normalization_rules.md
2stages/entity_v2_annotation_plan.md
```

清洗原则：

- 只改 assistant answer 里的 `[ENTITY]` 部分。
- 不改 `[RELATION]`。
- 不改 `[LOGIC_GROUP]`。
- 每个 relation 的 source/target 都必须出现在 entity 的 `normalized_name` 里。
- `normalized_name` 尽量短、稳定、可复用，不要写成长描述。

给 GPT 或人工标注者时，应该提供：

```text
1. 2stages/entity_v2_normalization_rules.md
2. 原始 SFT 行：system + user + assistant gold answer
3. 从原始 assistant [RELATION] 中解析出的 source/target 列表
```

不要只给 `outputs/` 里的模型预测结果。那些是错误分析参考，不是 gold 标签来源。

### 6.2 生成 entity_v2 训练数据

如果只是使用当前 prompt addendum 版本，可以直接用已有文件：

```text
2stages/data/entity_v2/combined_train_entity_v2_sft_messages.jsonl
2stages/data/entity_v2/combined_test_entity_v2_sft_messages.jsonl
```

但注意：这批文件目前主要是“prompt 加规则”的版本，不等于已经人工清洗过标签。

真正要突破瓶颈，需要把原始训练集标签清洗成 entity_v2 gold，然后再生成新的 entity SFT 数据。

原始训练集：

```text
D:\smartFTA\traindataset\combined_train_sft_messages.jsonl
```

原始测试集：

```text
D:\smartFTA\testdataset\combined_test_sft_messages.jsonl
```

测试集不要随便改成新 benchmark；可以用于诊断，但最终报告要说明清楚。

## 7. 服务器上怎么训练

服务器主目录：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft
```

确认模型路径：

```bash
export MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B
```

训练新的 entity_v2 adapter 示例：

```bash
python train_qwen3_14b_lora.py \
  --model-path /mnt/sda/huggingface/Qwen/Qwen3-14B \
  --train-file combined_train_entity_v2_sft_messages.jsonl \
  --eval-file combined_test_entity_v2_sft_messages.jsonl \
  --output-dir qwen3_14b_lora_runs/entity_v2_e4_lr8e5_r32_a64_len4096 \
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

训练时建议用 tmux：

```bash
tmux new -s qwen3-entity-v2
```

detach：

```text
Ctrl+b
d
```

重新进入：

```bash
tmux attach -t qwen3-entity-v2
```

## 8. 服务器上怎么启动推理服务

启动 entity_v2 adapter：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft
export MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B
export ADAPTER_PATH=qwen3_14b_lora_runs/entity_v2_e4_lr8e5_r32_a64_len4096
uvicorn serve_qwen3_14b_lora_gpu:app --host 0.0.0.0 --port 9100
```

启动 relation adapter：

```bash
cd ~/cufan/qwen3_14b
conda activate qwen-ft
export MODEL_PATH=/mnt/sda/huggingface/Qwen/Qwen3-14B
export ADAPTER_PATH=qwen3_14b_lora_runs/relation_e35_lr8e5_r32_a64_len4096
uvicorn serve_qwen3_14b_lora_gpu:app --host 0.0.0.0 --port 9100
```

检查服务：

```bash
curl http://127.0.0.1:9100/health
```

## 9. 本地怎么连接服务器

本地 Windows PowerShell 建 SSH 隧道：

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -L 29100:127.0.0.1:9100 yanhan-server
```

另开一个 PowerShell 测试：

```powershell
curl http://127.0.0.1:29100/health
```

如果使用别的本地端口，后面所有 `--endpoint` 都要同步改。

注意：

```text
服务器内部访问：127.0.0.1:9100
本地 Windows 访问：127.0.0.1:29100
```

不要混用。

## 10. 本地怎么跑两阶段测试

先跑 entity 阶段。此时服务器应启动 entity adapter。

```powershell
cd D:\smartFTA\baseline-remote-llm

python 2stages\run_two_stage_dataset.py `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_v2_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl `
  --endpoint http://127.0.0.1:29100/extract `
  --stage entity `
  --workers 2
```

再跑 relation 阶段。此时服务器应切换到 relation adapter。

```powershell
python 2stages\run_two_stage_dataset.py `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_v2_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl `
  --endpoint http://127.0.0.1:29100/extract `
  --stage relation `
  --workers 2
```

如果两个阶段都已经跑完，只想重新拼最终预测：

```powershell
python 2stages\run_two_stage_dataset.py `
  --input ..\testdataset\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_v2_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl `
  --stage stitch
```

## 11. 本地怎么评估

严格评估：

```powershell
python remote_llm_baseline\evaluate_span_relation.py `
  --input outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl `
  --output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_eval_report.json
```

语义评估：

```powershell
python remote_llm_baseline\evaluate_span_relation.py `
  --input outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl `
  --output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_semantic_eval_report.json `
  --semantic-normalized `
  --embedding-model D:\models\bge-m3 `
  --semantic-threshold 0.85
```

错误分析：

```powershell
python 2stages\analyze_entity_errors.py `
  --entity-output outputs\combined_test_twostage_entity_v2_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl `
  --out-json outputs\combined_test_twostage_entity_v2_error_analysis_qwen3_14b.json `
  --out-md outputs\combined_test_twostage_entity_v2_error_analysis_qwen3_14b.md `
  --top-k 30
```

评估完成后，把关键结果追加到：

```text
remote_llm_baseline/statistic.md
```

README 不再放大表格，避免统计结果在多个文件里漂移。

## 12. 判断下一步是否成功

不要只看 entity F1，要重点看：

```text
relation_endpoint_possible
relation strict F1
relation semantic F1
```

如果 entity_v2 后：

```text
relation_endpoint_possible 明显高于 315 / 625
relation strict F1 高于 0.4419
relation semantic F1 高于 0.5368
```

说明方向有效。

如果 entity F1 提升但 relation endpoint coverage 没提升，说明 normalized_name 仍然没有对齐 relation source/target，需要继续清洗 entity 规范。

## 13. 最短行动清单

1. 删除远端分支里错误残留的 `fta-visual-system/`。
2. 读 `remote_llm_baseline/README.md` 和 `remote_llm_baseline/statistic.md`。
3. 读 `2stages/entity_v2_normalization_rules.md` 和 `2stages/entity_v2_annotation_plan.md`。
4. 清洗训练集里的 `[ENTITY]` 标签，保证 relation source/target 都能在 entity normalized_name 中找到。
5. 生成新的 entity_v2 SFT 数据。
6. 在服务器训练新的 `entity_v2` adapter。
7. 用新 entity adapter + 旧 relation_e35 adapter 跑两阶段测试。
8. 跑严格评估、语义评估、错误分析。
9. 把指标写回 `remote_llm_baseline/statistic.md`。

