# Entity V2 Normalization Rules

## Purpose

This document is for annotators, data-cleaning scripts, prompt authors, and experiment reviewers. It is not the only thing the model sees during training.

Use it to keep the entity-stage target labels consistent. The model learns these rules through two paths:

1. The SFT assistant answers in `entity_v2` training data follow this document.
2. A short prompt addendum, `entity_v2_prompt_addendum.md`, is appended to the entity-stage system prompt during training and inference.

The immediate goal is benchmark-compatible entity extraction: improve relation candidate coverage for the current gold schema. Do not rewrite labels into a new ontology unless the train/test gold and relation labels are updated together.

## Current Bottleneck

From `outputs/combined_test_twostage_entity_error_analysis_qwen3_14b.json`:

| metric | value |
|---|---:|
| entity_normalized_f1 | 0.5971 |
| entity_mention_type_f1 | 0.6064 |
| gold_relations | 625 |
| relation_endpoint_possible | 315 |
| relation_endpoint_blocked | 310 |
| relation_endpoint_possible_recall_ceiling | 0.5040 |

This means many relation errors are decided before relation inference: the gold relation source/target names are absent from entity candidates.

## Core Principles

1. `mention` should stay close to the original text span.
2. `normalized_name` is the stable relation key. It must match the current gold style whenever possible.
3. Do not over-abstract specific gold names into generic terms.
4. Do not add measurement/status details to `normalized_name` unless the current gold keeps them.
5. Preserve relation endpoints. If a phrase appears as a gold relation source or target, it must exist as an entity candidate with the same `normalized_name`.
6. Keep `type` conservative. Wrong type blocks relation matching even when the name is close.
7. Ignore `start/end` quality for this iteration. Evidence text can remain noisy as long as entity keys improve.

## Type-Specific Rules

### 故障事件

Use component plus fault phenomenon. Preserve the object identity and important qualifiers used by gold.

Keep:

- equipment/component IDs that identify different objects: `1号`, `15#`, `TB1`, `RDS`, `MBX03CP005`
- direction/state words that change meaning: `无法`, `异常`, `泄漏`, `脱落`, `裂纹`, `磨损`, `烧蚀`, `干涉`
- severity when gold keeps it: `明显`, `轻微`, `严重`
- growth/change when gold keeps it: `轻微增长`, `无明显变化`, `扩展`

Avoid:

- replacing a concrete phenomenon with a broad category: `断断续续“嘣嘣”的声音` should not become `异响` if gold keeps the concrete symptom
- dropping object scope: `燃机透平部分动静叶片碰磨` should not become only `动静叶片碰磨`
- keeping raw measurement detail when gold removes it: `#14 涂层脱落 65mm` should normalize to `#14 涂层脱落`
- keeping verbose wording when gold uses a compact event: `橡胶O型圈起不到密封效果` should normalize to `橡胶O型圈密封失效`

Examples from current errors:

| mention | preferred normalized_name | avoid |
|---|---|---|
| 断断续续“嘣嘣”的声音 | 断断续续“嘣嘣”的声音 | 异响 |
| 燃机透平部分动静叶片碰磨 | 燃机透平部分动静叶片碰磨 | 动静叶片碰磨 |
| #14 涂层脱落 65mm | #14 涂层脱落 | #14 涂层脱落 65mm |
| #24 嵌件裂纹 32mm 23mm 轻微增长 | #24 嵌件裂纹轻微增长 | #24 嵌件裂纹 32mm 23mm 轻微增长 |
| 压气机第4级动叶近叶根部分积垢严重 | 压气机第4级动叶积垢 | 压气机第4级动叶近叶根部分积垢严重 |
| 影响燃机效率 | 燃机效率下降 | 影响燃机效率 |

### 维修方法

Use action plus object. Preserve meaningful operation scope and modifiers.

Keep:

- action verbs: `检查`, `更换`, `清理`, `打磨`, `补焊`, `探伤`, `调整`, `观察`
- target object: `瓦块位置`, `15#燃烧器嵌件`, `气膜孔`, `滤网`
- meaningful modifiers in gold: `重新`, `用新备件`, `现场目视`

Avoid:

- deleting a modifier kept by gold: `重新调整瓦块位置` should not become `调整瓦块位置`
- changing aspect casually: `更换金属导流瓦` should not become `更换了金属导流瓦`
- keeping filler if gold compacts it: `暂无需处理，保持观察` often normalizes to `保持观察`

Examples:

| mention | preferred normalized_name | avoid |
|---|---|---|
| 重新调整瓦块位置 | 重新调整瓦块位置 | 调整瓦块位置 |
| 用新备件更换15#燃烧器嵌件 | 用新备件更换15#燃烧器嵌件 | 更换15#燃烧器嵌件 |
| 更换金属导流瓦 | 更换金属导流瓦 | 更换了金属导流瓦 |
| 暂无需处理，保持观察 | 保持观察 | 暂无需处理，保持观察 |
| 底下部分磨了点坡口 | 打磨底部坡口 | 磨坡口 |

### 触发规则

Use a condition expression, not just the parameter name.

Keep:

- comparison words: `高于`, `低于`, `超过`, `达到`, `不满足`
- threshold or setting when present
- parameter/device name

Avoid:

- dropping the comparator: `低于压力开关MBX03CP005整定值` should not become `压力开关MBX03CP005整定值`
- converting rule into fault event unless it is described as an observed fault

### 报警码

Use the alarm/code identifier only.

Keep:

- alarm code tokens: `F01000`, `F.EGDB.01`, `MBX03CP005`

Remove:

- observed values after `=`
- units and readings
- explanatory text around the code

Example:

| mention | preferred normalized_name | avoid |
|---|---|---|
| F.EGDB.01=0.055kg/s | F.EGDB.01 | F.EGDB.01=0.055kg/s |

### 故障类别

Use broad, stable category labels only. Do not create a category if the text only states a concrete event.

## Relation-Coverage Rule

Before finalizing an entity target, check gold relation endpoints for the same row:

1. Every relation `source` must be present in `[ENTITY]` as a `normalized_name`.
2. Every relation `target` must be present in `[ENTITY]` as a `normalized_name`.
3. If an endpoint is absent, either add the missing entity or align the existing entity's `normalized_name` to the endpoint.

This is the most important entity_v2 rule because relation inference uses `[ENTITY_CANDIDATES]` as a closed set.

## Review Priority

Focus manual review on these error groups first:

1. Rows with many blocked gold relations: `162`, `7`, `23`, `150`, `197`, `34`, `59`.
2. Entity types with large missing/extra counts:
   - `故障事件`: missing `179`, extra `246`
   - `维修方法`: missing `141`, extra `147`
   - `触发规则`: missing `17`, extra `12`
3. Near-miss normalized pairs where mention is correct but normalized_name differs.

## Acceptance Targets For Entity V2

Entity_v2 is useful only if it improves relation endpoint coverage.

Minimum target:

- `relation_endpoint_possible` should improve from `315 / 625` to at least `400 / 625`.
- `entity_normalized_f1` should exceed `0.6167`, the best current checkpoint.

Strong target:

- `relation_endpoint_possible` exceeds `480 / 625`.
- End-to-end two-stage relation strict F1 clearly exceeds `0.4469`.

