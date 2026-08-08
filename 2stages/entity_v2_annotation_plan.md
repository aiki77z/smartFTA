# Entity V2 Annotation Plan

## Goal

Create a label-cleaned entity dataset that improves entity candidate coverage for relation extraction.

The target is not prettier labels. The target is better relation endpoint coverage:

- Baseline `relation_endpoint_possible`: `315 / 625`
- Minimum entity_v2 target: `400 / 625`
- Strong entity_v2 target: `480 / 625`

## What To Give A Large Model For Cleaning

Give the model all of these:

1. `2stages/entity_v2_normalization_rules.md`
2. The original SFT row, including:
   - `messages[0]` system prompt
   - `messages[1]` user text
   - original assistant gold answer
3. The parsed gold relation endpoints for that row:
   - every `[RELATION]` source
   - every `[RELATION]` target

Do not give only entity-stage prediction rows. Those are model outputs, not gold labels.

## What The Model Should Edit

For each row, edit only the `[ENTITY]` section of the assistant answer.

Allowed edits:

- add missing entities needed by relation endpoints
- remove duplicated or clearly spurious entity lines
- fix `type`
- fix `normalized_name`
- keep or lightly clean `mention`
- keep evidence as-is unless it is obviously attached to the wrong entity

Disallowed edits:

- do not rewrite `[RELATION]`
- do not rewrite `[LOGIC_GROUP]`
- do not invent entities without evidence in `context_text` or `text`
- do not change the output format
- do not add explanations, JSON, markdown, or comments inside the cleaned assistant answer

## Required Coverage Check

Before accepting a cleaned row:

1. Collect all entity `normalized_name` values.
2. Collect all relation source/target names.
3. Every relation source/target must appear in entity `normalized_name`.
4. If not, add the missing entity or align an existing entity normalized_name.

This is the main acceptance rule.

## Recommended Cleaning Workflow

### Pass 1: High-Impact Rows

Start with rows that block the most gold relations:

```text
162, 7, 23, 150, 197, 34, 59, 46, 129, 160
```

Use:

```text
outputs/combined_test_twostage_entity_error_analysis_qwen3_14b.json
```

as a diagnostic reference only. Do not include `outputs/` in repo handoff.

### Pass 2: Type-Specific Cleanup

Prioritize:

1. `故障事件`
2. `维修方法`
3. `触发规则`
4. `报警码`
5. `故障类别`

### Pass 3: Full Train Set

After the rules are stable, clean the training split first:

```text
..\traindataset\combined_train_sft_messages.jsonl
```

Then clean the test split only for diagnostic oracle-style experiments, not for final held-out reporting unless you intentionally define a new cleaned benchmark.

## Prompt Template For GPT Cleaning

```text
You are cleaning SmartFTA entity labels.

Read the normalization rules below.
Then read one SFT row containing system, user, and assistant gold answer.

Task:
- Rewrite only the [ENTITY] section in the assistant answer.
- Keep [RELATION] and [LOGIC_GROUP] exactly unchanged.
- Ensure every relation source/target appears as an entity normalized_name.
- Preserve the original three-section format.
- Output only the cleaned assistant answer.

[NORMALIZATION_RULES]
{paste entity_v2_normalization_rules.md}

[SFT_ROW]
{paste one original JSONL row or a readable rendering of system/user/assistant}

[RELATION_ENDPOINTS]
{paste source/target list parsed from the assistant [RELATION] section}
```

## Expected Output

The model must output:

```text
[ENTITY]
mention | type | normalized_name | evidence
...

[RELATION]
...

[LOGIC_GROUP]
...
```

No commentary.

## Validation Before Training

After cleaning, run a validation script to check:

- JSONL is valid
- every row has `system`, `user`, `assistant`
- entity lines have at least 4 fields
- relation source/target endpoint coverage
- entity type is one of the 5 allowed types
- relation type is one of the allowed relation types

Only then build entity-only SFT data and train `entity_v2`.

