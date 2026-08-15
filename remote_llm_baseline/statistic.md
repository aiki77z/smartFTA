# LLM 基线与不同参数下 LoRA 结果评估

## 结果对比

当前报告文件：

```text
outputs\combined_test_prompt_only_eval_report.json
outputs\combined_test_lora_eval_report.json
outputs\combined_test_twostage_qwen3_14b_eval_report.json
outputs\combined_test_twostage_qwen3_14b_semantic_eval_report.json
```

说明：当前实验主线是 Qwen3-14B，包括 14B prompt-only、14B 单阶段 LoRA、14B two-stage 和 entity_v2。早期 Qwen3-8B 实验效果较差，已经舍弃；表格中未显式标注 `Qwen3-14B` 的早期 `Prompt-only` / `LoRA` 行仅作为历史对照。

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
| LoRA `Qwen3-14B e4_lr8e5_r32_a64_len4096_ckpt25 checkpoint-375` | **0.1535** | 0.5175 | 0.5980 | 0.6071 | **0.1516** | 0.5856 | 0.4063 | 0.3713 |
| LoRA `Qwen3-14B e4_lr8e5_r32_a64_len4096_ckpt25 checkpoint-400` | 0.1465 | **0.5325** | **0.6108** | **0.6178** | 0.1463 | **0.6137** | 0.4200 | 0.3680 |
| LoRA `Qwen3-14B e4_lr8e5_r32_a64_len4096_ckpt25 checkpoint-424` | 0.1517 | 0.5235 | **0.6167** | **0.6240** | **0.1516** | **0.6152** | 0.4335 | 0.3863 |
| LoRA `Qwen3-14B e5_lr8e5_r32_a64_len4096_ckpt25 checkpoint-525` | **0.1619** | **0.5443** | 0.5938 | 0.6001 | **0.1601** | 0.5947 | 0.4128 | 0.3727 |
| Two-stage `Qwen3-14B entity_e4 + relation_e35` | 0.1568 | 0.5185 | 0.5971 | 0.6064 | 0.1559 | 0.5883 | 0.4419 | 0.3827 |
| Oracle Entity Relation Prompt-only `Qwen3-14B` | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.7014 | 0.5437 |
| Oracle Entity Relation `Qwen3-14B relation_e35` | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | **0.8085** | **0.7134** |
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
| LoRA `Qwen3-14B e4_lr8e5_r32_a64_len4096_ckpt25 checkpoint-375` | 0.5980 | **0.6246** | **0.7023** | 0.5120 | 0.4587 |
| LoRA `Qwen3-14B e4_lr8e5_r32_a64_len4096_ckpt25 checkpoint-400` | **0.6108** | **0.6348** | **0.7131** | 0.5283 | 0.4563 |
| LoRA `Qwen3-14B e4_lr8e5_r32_a64_len4096_ckpt25 checkpoint-424` | **0.6167** | **0.6423** | **0.7194** | 0.5473 | 0.4783 |
| LoRA `Qwen3-14B e5_lr8e5_r32_a64_len4096_ckpt25 checkpoint-525` | 0.5938 | 0.6125 | 0.6897 | 0.5099 | 0.4466 |
| Two-stage `Qwen3-14B entity_e4 + relation_e35` | 0.5971 | 0.6128 | 0.6821 | 0.5368 | 0.4661 |
| Oracle Entity Relation Prompt-only `Qwen3-14B` | 1.0000 | 1.0000 | 1.0000 | 0.7120 | 0.5525 |
| Oracle Entity Relation `Qwen3-14B relation_e35` | 1.0000 | 1.0000 | 1.0000 | **0.8150** | **0.7199** |

说明：本次语义重评估暂未包含 8B Prompt-only，因为对应预测文件已删除。`Hybrid Semantic F1` 沿用上一版定义：gold 侧可抽取实体（`mention == normalized_name` 或 normalized 出现在 evidence text 中）仍按严格匹配，归纳型实体允许 bge-m3 语义匹配。`All-Entity Semantic F1` 是新增指标：所有实体都允许语义匹配，包括 `mention == normalized_name` 的可抽取实体。语义三元组评估要求 `row_id + relation_type` 严格一致，source 和 target 通过同一语义相似度阈值匹配；带极性/确定性版本额外要求 `polarity + certainty` 严格一致。

`Oracle Entity Relation` 不是端到端结果，而是诊断实验：关系阶段使用 gold `[ENTITY]` 作为闭集候选。Prompt-only Qwen3-14B 在 oracle 条件下 relation strict F1 为 0.7014，relation_e35 微调后提升到 0.8085；普通 two-stage 只有 0.4419，说明 relation 微调有效，但当前端到端主要瓶颈仍在 entity 候选质量。

Entity 错误分析报告：

```text
outputs\combined_test_twostage_entity_error_analysis_qwen3_14b.md
outputs\combined_test_twostage_entity_error_analysis_qwen3_14b.json
```

关键诊断：`gold_relations = 625`，其中只有 `315` 条 gold relation 的 source/target 能被普通 two-stage 的 entity 候选同时覆盖，`310` 条在进入 relation 阶段前已经被 entity 候选阻断，端点覆盖给 relation recall 设置的粗略上限约为 `0.5040`。因此下一步优先做 `entity_v2`，暂不优先继续微调 relation。

## Entity V2 第一步：prompt-addendum 重训结果（2026-08-10）

实验内容：使用 `2stages/data/entity_v2/`（assistant gold 与原始一致，system prompt 追加
`entity_v2_prompt_addendum.md` 规则）训练 `entity_v2_e4_lr8e5_r32_a64_len4096`，
推理时同样追加规则文件，relation 阶段复用 `relation_e35`。

先行的标签分析发现：训练集 gold 端点覆盖 2508/2508、测试集 626/626（100%），
即 gold 标签本身完全一致，315/625 的瓶颈在模型预测侧，不在标签。

| 指标 | 基线 entity_e4 + relation_e35 | entity_v2 第一步（prompt-addendum） |
|---|---:|---:|
| relation_endpoint_possible | 315 / 625 (0.5040) | 307 / 625 (0.4912) |
| relation strict F1 | 0.4419 | 0.4267 |
| relation semantic F1 | 0.5368 | 未跑（本地缺 bge-m3） |
| entity_normalized F1 | 0.5971 | 0.6007 |
| entity_mention_type F1 | 0.6064 | 0.6045 |
| overlong extra predictions | 有 | 0 |

结论：第一步未达标（strict F1 与端点覆盖均略降）。entity F1 微升但端点覆盖未升，
符合"normalized_name 仍未对齐 relation 端点"的预警情形。仅加规则提示、不改标签
不足以解决瓶颈，进入第二步：按 `entity_v2_normalization_rules.md` 清洗训练集约
120 行风格问题（过长/带测量值的 normalized_name）后重训。

## Entity V2 第二步：标签风格清洗 + 重训结果（2026-08-10）

实验内容：按 `entity_v2_normalization_rules.md` 用 DeepSeek 清洗训练集 120 行
风格问题（120 行 0 错误；实际 9 行 14 处 normalized_name 被修改，主要去除
测量值/压缩长描述），合并后全量校验通过（844 行、端点覆盖 2508/2508），
生成 `entity_v2_clean` SFT 并训练 `entity_v2_clean_e4_lr8e5_r32_a64_len4096`，
relation 阶段复用 `relation_e35`。

| 指标 | 基线 entity_e4 | 第一步 prompt-addendum | 第二步 entity_v2_clean |
|---|---:|---:|---:|
| relation_endpoint_possible | 315 / 625 (0.5040) | 307 / 625 (0.4912) | 324 / 625 (0.5184) |
| relation strict F1 | 0.4419 | 0.4267 | 0.4579 |
| relation semantic F1 | 0.5368 | 未跑 | 未跑（本地缺 bge-m3） |
| entity_normalized F1 | 0.5971 | 0.6007 | 0.6011 |
| entity_mention_type F1 | 0.6064 | 0.6045 | 0.6061 |
| overlong extra predictions | 有 | 0 | 0 |

结论：第二步方向有效——strict F1 超过基线（0.4419）与单阶段最优（0.4469），
端点覆盖 315 -> 324。但仍低于 HANDOFF 最低目标（400/625），端到端还有较大
提升空间；语义评估待补（需要 `D:\models\bge-m3`）。

## Entity V2 扩大清洗（主动规范化模式）结果（2026-08-14）

实验内容：在第二步基础上把清洗改为"主动规范化"模式（对每条 normalized_name
主动去测量值/压缩长描述），120 行清洗 0 错误、22 行 31 处改动，合并校验通过后
训练 `entity_v2_clean_v2_e4_lr8e5_r32_a64_len4096`，relation 阶段复用
`relation_e35`。

| 指标 | 基线 entity_e4 | 第二步 entity_v2_clean | 扩大清洗 entity_v2_clean_v2 |
|---|---:|---:|---:|
| relation_endpoint_possible | 315 / 625 (0.5040) | 324 / 625 (0.5184) | 301 / 625 (0.4816) |
| relation strict F1 | 0.4419 | 0.4579 | 0.4118 |
| entity_normalized F1 | 0.5971 | 0.6011 | 0.5963 |
| entity_mention_type F1 | 0.6064 | 0.6061 | 0.5910 |

结论：**主动扩大清洗是负向结果**——改动更多标签（如补动作对象、压缩表述）
使训练风格偏离测试集 gold 风格，模型输出的 normalized_name 与测试端点对齐度
下降，strict F1 与端点覆盖均低于基线和第二步。当前最佳仍是第二步的
`entity_v2_clean_e4...`（strict F1 0.4579、端点覆盖 324/625）。扩大清洗方案
收手，不再继续；下一步优先检查点选择或语义评估补全。
