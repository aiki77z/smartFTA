# SmartFTA baseline-remote-llm 工作进展说明

> 本文件记录针对原始仓库（`https://github.com/aiki77z/smartFTA` 的
> `baseline-remote-llm` 分支）已进行的检查、分析与改动，对照 `HANDOFF.md`
> 标记完成状态，并列出后续待办。

## 1. 项目来源与当前状态

- 工作区：`D:\classes\waibao\FTA-NEW\baseline_llm`。
- 来源：克隆 `smartFTA` 仓库 `baseline-remote-llm` 分支，当前 HEAD 为
  `bfed3aa`（"Remove visual system from remote LLM baseline branch"）。
- 服务器访问：SSH 别名 `yanhan-server`（`202.120.40.86:1261`，用户 `yanhan`），
  项目目录 `~/cufan/qwen3_14b`，conda 环境 `qwen-ft`，基座模型
  `/mnt/sda/huggingface/Qwen/Qwen3-14B`。
- 本地新增内容：
  - `2stages/entity_v2_tools/`：清洗与分析工具链（新增，未提交）。
  - `data_raw/`：从服务器拉取的原始 SFT 数据本地副本（train 6.7MB、test 1.7MB）。
  - `outputs/entity_v2_clean/`：覆盖度/风格分析报告与清洗队列（Git 已忽略）。

## 2. 服务器体检结果（已完成）

| 检查项 | 结果 |
|---|---|
| 项目文件 | `train_qwen3_14b_lora.py`、`serve_qwen3_14b_lora_gpu.py`、`qwen3_14b_lora_runs/` 均在 |
| torch/CUDA | 2.6.0+cu124，CUDA 可用，NVIDIA A100 80GB |
| 基座模型 | `/mnt/sda/huggingface/Qwen/Qwen3-14B` 完整 |
| adapter | `entity_e4_lr8e5_r32_a64_len4096`、`relation_e35_lr8e5_r32_a64_len4096` 等均在 |
| GPU 占用 | `app_api.py`（PID 1656169，yanhan，约 30GB）在跑，**不要杀**；剩余约 50GB 足够 |
| 9100 端口 | 无服务（可放心使用） |
| 磁盘 | 根分区约 73GB 可用；`/mnt/sda` 约 9.4TB |
| 服务器数据 | 原始 `combined_train/test_sft_messages.jsonl` 及 v1 entity/relation 文件已在 `~/cufan/`；**entity_v2 数据尚未上传** |

## 3. 已完成的本地工具与分析

### 3.1 工具链（`2stages/entity_v2_tools/`）

| 脚本 | 作用 | 状态 |
|---|---|---|
| `common.py` | 共享解析（SFT 行、三段落、实体/关系/逻辑组） | 已用合成数据验证 |
| `analyze_endpoint_coverage.py` | 计算 gold 关系端点覆盖 + 输出清洗队列 | 已跑通 |
| `generate_cleaning_inputs.py` | 生成清洗输入（规则+原始行+关系端点，含 prompt） | 已跑通 |
| `merge_cleaned_answers.py` | 清洗结果合并回原始格式 | 已跑通 |
| `validate_entity_v2_clean.py` | 清洗后校验（字段/类型/端点覆盖），未通过退出码 1 | 正反用例均验证 |
| `analyze_label_style.py` | 标签风格扫描（过长、带测量值、重复等） | 已跑通 |
| `README.md` | 工具链使用说明 | 已写 |

### 3.2 关键分析结论

- **端点覆盖（gold 层）**：
  - 训练集：844 行、4050 实体、2508 条关系，端点覆盖 **2508/2508（100%）**。
  - 测试集：213 行、962 实体、626 条关系，端点覆盖 **626/626（100%）**。
  - 结论：gold 标签本身完全一致。HANDOFF 中 `315/625` 的瓶颈在**模型预测侧**
    （entity adapter 输出的 `normalized_name` 与 gold 不一致），不是标签缺端点。
    因此"清洗标签补端点"无意义；后续验收指标必须基于**重训后的模型预测**计算。
- **风格扫描**：
  - 训练集：120 行需要关注（117 个过长 normalized_name、102 个疑似带测量值）。
  - 测试集：28 行需要关注（29 个过长、30 个疑似带测量值、1 个重复）。
  - "疑似带测量值"是启发式检测，会把设备编号（如 `MBX03CP002`）误判，实际数量
    是上界；**过长 normalized_name 是更可靠的清洗目标**。
  - 队列已生成：`outputs/entity_v2_clean/train_style_queue.jsonl`、
    `outputs/entity_v2_clean/test_style_queue.jsonl`。

## 4. HANDOFF 完成情况对照

对照 `HANDOFF.md` 第 13 节"最短行动清单"：

| # | 行动 | 状态 |
|---|---|---|
| 1 | 删除远端分支残留的 `fta-visual-system/` | ✅ 已完成（上游提交 `bfed3aa`，克隆分支已无此目录） |
| 2 | 读 `README.md` 和 `statistic.md` | ✅ 已完成 |
| 3 | 读 normalization_rules 和 annotation_plan | ✅ 已完成 |
| 4 | 清洗训练集 `[ENTITY]` 标签 | ✅ 已完成：端点覆盖本就 100%，无需补端点；风格清洗 120 行，温和模式 9 行 14 处、主动规范化模式 22 行 31 处（均 0 错误，改动有原文依据） |
| 5 | 生成 entity_v2 SFT 数据 | ✅ 已完成：prompt-addendum 版已有；`entity_v2_clean` 版已生成于 `2stages/data/entity_v2_clean/` |
| 6 | 服务器训练 entity_v2 adapter | ✅ 已完成（`entity_v2_e4...` 与 `entity_v2_clean_e4...` 均已训练，2026-08-10） |
| 7 | 新 entity adapter + 旧 relation_e35 跑两阶段 | ✅ 已完成（两轮 entity/relation/stitch 均 213 行、0 error） |
| 8 | 严格/语义/错误分析 | ⚠️ 部分完成：严格评估与错误分析已跑；语义评估缺本地 bge-m3（`D:\models\bge-m3` 不存在） |
| 9 | 指标写回 `statistic.md` | ✅ 已完成（追加"Entity V2 第一步"结果节） |

当前已知基线（来自 `remote_llm_baseline/statistic.md`）：

- 两阶段 `entity_e4 + relation_e35`：relation strict F1 = 0.4419，semantic F1 = 0.5368。
- Oracle（gold entity）：relation strict F1 = 0.8085，semantic = 0.8150。
- 待超越目标：`relation_endpoint_possible` 从 315/625 明显提升、strict F1 > 0.4419、
  semantic F1 > 0.5368。

### 第一步实验结果（2026-08-10，prompt-addendum 重训）

| 指标 | 基线 | 第一步结果 | 变化 |
|---|---:|---:|---|
| relation_endpoint_possible | 315/625 (0.5040) | 307/625 (0.4912) | ↓ |
| relation strict F1 | 0.4419 | 0.4267 | ↓ |
| entity_normalized F1 | 0.5971 | 0.6007 | ↑ 微 |
| entity_mention_type F1 | 0.6064 | 0.6045 | ↓ 微 |
| overlong extra predictions | 有 | 0 | 改善 |
| relation semantic F1 | 0.5368 | 未跑（缺 bge-m3） | - |

结论：第一步**未达标**。仅加规则提示、不改标签不足以让模型输出对齐 relation 端点。
按计划进入第二步：清洗训练集约 120 行风格问题后重训。

### 第二步实验结果（2026-08-10，标签风格清洗 + 重训）

| 指标 | 基线 | 第一步 | 第二步 | 目标 |
|---|---:|---:|---:|---:|
| relation_endpoint_possible | 315/625 (0.5040) | 307/625 (0.4912) | 324/625 (0.5184) | ≥400/625 |
| relation strict F1 | 0.4419 | 0.4267 | 0.4579 | >0.4419 |
| relation semantic F1 | 0.5368 | 未跑 | 未跑 | >0.5368 |
| entity_normalized F1 | 0.5971 | 0.6007 | 0.6011 | - |
| entity_mention_type F1 | 0.6064 | 0.6045 | 0.6061 | - |

结论：第二步**方向有效但未达最低目标**——strict F1 超过基线与单阶段最优
（0.4469），端点覆盖 315 -> 324；但离 400/625 还有距离。语义评估缺 bge-m3 待补。

## 5. 后续计划：第一步与第二步

### 第一步：prompt-addendum 重训（只改提示，不改标签）

- 数据：仓库自带 `2stages/data/entity_v2/combined_train_entity_v2_sft_messages.jsonl`
  （844 行）。assistant gold 与原始训练集**完全一致**，只是 system prompt 追加了
  `entity_v2_prompt_addendum.md` 的归一化规则。
- 含义：验证"训练/推理时追加归一化规则"能否让模型输出的 `normalized_name` 更贴近
  gold 风格，从而提升**预测侧**端点覆盖。
- 成本：无需人工清洗，数据现成；训练超参与 entity_e4 一致。
- 操作：scp 两个 entity_v2 文件到服务器 → 训练 `entity_v2_e4_lr8e5_r32_a64_len4096`
  → 起服务 → 本地跑两阶段（entity_v2 + 旧 relation_e35）→ 严格/语义评估。

### 第二步：标签风格清洗 + 重训（改标签）

- 数据：按 `entity_v2_normalization_rules.md` 清洗训练集约 120 行（主要是过长的
  normalized_name、带测量值的表述），通过工具链生成 `entity_v2_clean` SFT 数据。
- 含义：在第一步基础上验证"训练目标更一致"能否进一步提升预测侧端点覆盖。
- 前置：先跑第一步；若第一步已达标，第二步可跳过或作为增强。
- 操作：`generate_cleaning_inputs.py` 生成清洗任务 → GPT/人工清洗 →
  `merge_cleaned_answers.py` 合并 → `validate_entity_v2_clean.py` 校验 →
  `build_two_stage_datasets.py` 生成 SFT → 服务器重训 → 两阶段评估。

### 两步的区别

| 维度 | 第一步 | 第二步 |
|---|---|---|
| 改什么 | 只改 system prompt（追加规则） | 改 assistant gold 的 `[ENTITY]` 标签（保留 RELATION/LOGIC_GROUP） |
| 数据来源 | 现有 prompt-addendum 文件 | 清洗后新生成的 `entity_v2_clean` 文件 |
| 需要人工清洗 | 否 | 是（约 120 行） |
| 验证的问题 | 规则提示能否改善归一化 | 训练目标一致性能否进一步改善归一化 |
| 成本 | 低（直接训练） | 中（清洗+校验+重建数据） |
| 建议顺序 | 先做 | 后做（依赖第一步结果） |

## 6. 复现命令速查

详细命令见：

- 工具链使用：`2stages/entity_v2_tools/README.md`
- 服务器训练/服务/两阶段/评估：`HANDOFF.md` 第 7-11 节、`remote_llm_baseline/README.md` 第 15 节

关键步骤：

```bash
# 服务器训练 entity_v2 adapter
cd ~/cufan/qwen3_14b && conda activate qwen-ft
python train_qwen3_14b_lora.py \
  --model-path /mnt/sda/huggingface/Qwen/Qwen3-14B \
  --train-file ../combined_train_entity_v2_sft_messages.jsonl \
  --eval-file ../combined_test_entity_v2_sft_messages.jsonl \
  --output-dir qwen3_14b_lora_runs/entity_v2_e4_lr8e5_r32_a64_len4096 \
  --max-length 4096 --num-train-epochs 4 --learning-rate 8e-5 \
  --per-device-train-batch-size 1 --gradient-accumulation-steps 8 \
  --lora-r 32 --lora-alpha 64 --save-steps 50 --eval-steps 50 --logging-steps 5
```

```powershell
# 本地跑两阶段（entity 阶段）
python 2stages\run_two_stage_dataset.py `
  --input data_raw\combined_test_sft_messages.jsonl `
  --entity-output outputs\combined_test_twostage_entity_v2_qwen3_14b.jsonl `
  --relation-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b.jsonl `
  --final-output outputs\combined_test_twostage_entity_v2_relation_e35_qwen3_14b_predictions.jsonl `
  --endpoint http://127.0.0.1:29100/extract `
  --stage entity --workers 2 --entity-rules-file 2stages\entity_v2_prompt_addendum.md
```

## 7. 待办清单（按优先级）

1. ✅ （第一步）训练 `entity_v2_e4_lr8e5_r32_a64_len4096` 并完成两阶段评估（未达标）。
2. ✅ （第二步）清洗 120 行 + 训练 `entity_v2_clean_e4_lr8e5_r32_a64_len4096`
   并完成两阶段评估（strict F1 0.4579、端点覆盖 324/625，方向有效）。
3. ✅ （扩大清洗实验）主动规范化模式 120 行（22 行 31 处改动）已训练
   `entity_v2_clean_v2` 并重测：**负向结果**（strict F1 0.4118、端点覆盖
   301/625，低于第二步）。扩大清洗方案收手；当前最佳为第二步
   `entity_v2_clean_e4...`（0.4579 / 324）。
4. ⬜ 语义评估未跑（本地缺 bge-m3，需先下载或改用其他路径）。
5. ⬜ 可选：检查点选择（`entity_v2_clean` 的中间 checkpoint）/ 评估 relation
   阶段 / 细化归一化规则，冲击
   `relation_endpoint_possible ≥ 400/625`。
6. ⬜ 最终指标写回 `remote_llm_baseline/statistic.md`（第一、二步结果已追加，
   语义评估结果待补）。

## 8. 接入 smartFTA/FTA-KB 的工作进展（2026-08-15）

### 8.1 目标

把当前效果最好的两阶段模型接入主项目 `smartFTA` 的知识库构建模块 `FTA-KB`，
作为**可选开关**使用，不删除原有单阶段 DeepSeek 逻辑。

最佳模型组合：

```text
entity adapter:   qwen3_14b_lora_runs/entity_v2_clean_e4_lr8e5_r32_a64_len4096
relation adapter: qwen3_14b_lora_runs/relation_e35_lr8e5_r32_a64_len4096
```

主项目本地路径：

```text
D:\classes\waibao\FTA-NEW\smartFTA
├── FTA-GNR
├── FTA-KB
└── fta-visual-system
```

### 8.2 接入点结论

模型接入点确定为 `FTA-KB/llm_annotation_extractor.py` 的 LLM 标注抽取阶段，
而不是 `FTA-GNR/generator.py` 的故障树 JSON 生成阶段。

FTA-KB 当前链路：

```text
main.py 上传/任务入口
  → kb_pipeline_v2.py 子进程
  → llm_annotation_extractor.generate_annotation_dataset()
  → LLM 抽取 [ENTITY] / [RELATION] / [LOGIC_GROUP]
  → 实体聚类、关系聚合
  → MongoDB / Neo4j
```

### 8.3 FTA-KB 本地新增/修改内容

新增文件：

```text
FTA-KB/llm_ft_extractor.py
FTA-KB/entity_v2_prompt_addendum.md
FTA-KB/.env
FTA-KB/TWO_STAGE_LORA_INTEGRATION.md
```

修改文件：

```text
FTA-KB/llm_annotation_extractor.py
```

修改方式为“仅增分支”，原单阶段逻辑未删除：

```python
if os.getenv("LLM_MODE", "single") == "two-stage":
    from llm_ft_extractor import generate_annotation_dataset_two_stage
    return generate_annotation_dataset_two_stage(...)
```

`.env` 关键配置：

```env
LLM_MODE=two-stage
LLM_FT_BASE_URL=http://127.0.0.1:29100/v1
LLM_FT_API_KEY=EMPTY
LLM_FT_ENTITY_MODEL=qwen3-entity
LLM_FT_RELATION_MODEL=qwen3-relation
LLM_FT_ENTITY_RULES_FILE=entity_v2_prompt_addendum.md
MONGO_URI=mongodb://127.0.0.1:27017/
MONGO_DB_NAME=fault-tree-trial
```

### 8.4 服务器侧部署

服务器新增：

```text
~/cufan/qwen3_14b/serve_two_stage_proxy.py
```

启动三个 tmux 服务：

```text
entity-service   → 9100，entity_v2_clean_e4...
relation-service → 9101，relation_e35...
ft-proxy         → 9200，OpenAI 兼容代理
```

代理模型映射：

```text
qwen3-entity   → http://127.0.0.1:9100/extract
qwen3-relation → http://127.0.0.1:9101/extract
```

本地 SSH 隧道：

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -L 29100:127.0.0.1:9200 yanhan-server
```

### 8.5 测试结果

使用 `FTA-KB/test.pdf` 跑通两阶段流水线：

```text
mineru:      成功，生成 26 个 chunk
mongodb:     成功导入 26 chunks
llm_extraction:
  samples: 26
  failed: 0
clustering:  成功，mentions=378，clusters=328
最终状态:    status=success
```

产物目录：

```text
FTA-KB/output_pdf_test/test/test_v1/
```

关键产物：

```text
test_chunks.json
test.csv
test_raw_llm.jsonl
entity_clustering/cluster_intermediate.json
```

### 8.6 本次测试中解决/确认的问题

| 问题 | 结论/处理 |
|---|---|
| MinerU `WinError 1314` | Windows 未开启开发者模式，无法创建符号链接；开启开发者模式或管理员运行 |
| LLM 抽取 `502` | 服务重启后代理与 entity 均返回 200；后续如再出现需检查代理/上游日志 |
| `version directory already exists and is not empty` | 上次运行残留旧产物目录；换输出目录或清理旧目录 |
| `context=False` | 正常，表示当前 chunk 未使用上一 chunk 作为上下文 |

### 8.7 待办与后续优化

1. ⬜ 若正式评估时指标下降，优先把 `llm_ft_extractor.py` 的提示词改为与训练时一致的
   system/user 分离格式。
2. ⬜ 完整入库流程尚未跑：本次使用 `--embedding-backend none --skip-cross-file-import`，
   后续需补 embedding 与 Neo4j 配置后验证端到端入库。
3. ⬜ 可选：entity 多 adapter 候选并集，提升关系端点覆盖（模拟可从 324 提升到约 374）。
4. ⬜ 语义评估仍未补跑（本地缺 bge-m3）。
5. ✅ 接入文档已写入
   `FTA-KB/TWO_STAGE_LORA_INTEGRATION.md`。
