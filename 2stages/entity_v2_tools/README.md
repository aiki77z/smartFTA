# Entity V2 Cleanup Toolchain

这些脚本用于路径 A：清洗原始 SFT 训练集的 `[ENTITY]` 标签，生成真正的
label-cleaned entity_v2 数据，并校验后再生成 SFT 训练文件。

## 流程

```text
① 取数：scp 原始 train/test 到 data_raw/
② 覆盖度基线：analyze_endpoint_coverage.py  -> 复现 315/625 类基线 + 清洗队列
③ 清洗输入：generate_cleaning_inputs.py     -> cleaning_inputs.jsonl
④ 清洗：GPT/人工按 prompt 字段清洗，输出 cleaned_answers.jsonl
⑤ 合并：merge_cleaned_answers.py            -> entity_v2_clean 原始格式文件
⑥ 校验：validate_entity_v2_clean.py         -> 必须 PASSED
⑦ 生成 SFT：build_two_stage_datasets.py     -> entity_v2_clean SFT 文件
⑧ 上传服务器训练 entity_v2 adapter
```

## ① 取数（本地 PowerShell）

```powershell
New-Item -ItemType Directory -Force data_raw
scp -P 1261 yanhan@202.120.40.86:~/cufan/combined_train_sft_messages.jsonl .\data_raw\
scp -P 1261 yanhan@202.120.40.86:~/cufan/combined_test_sft_messages.jsonl .\data_raw\
```

## ② 覆盖度基线 + 清洗队列

```powershell
python 2stages\entity_v2_tools\analyze_endpoint_coverage.py `
  --input data_raw\combined_train_sft_messages.jsonl `
  --out-json outputs\entity_v2_clean\train_coverage_report.json `
  --out-md outputs\entity_v2_clean\train_coverage_report.md `
  --queue outputs\entity_v2_clean\train_cleaning_queue.jsonl
```

输出 `queue` 里是需要清洗的行（有 relation 端点缺失），按阻断关系数降序，
这就是 Pass 1 的工作顺序。

## ③ 清洗输入

```powershell
python 2stages\entity_v2_tools\generate_cleaning_inputs.py `
  --input data_raw\combined_train_sft_messages.jsonl `
  --rules-file 2stages\entity_v2_normalization_rules.md `
  --out-jsonl 2stages\data\entity_v2_clean\cleaning_inputs.jsonl `
  --only-blocked
```

每行包含 `prompt`（规则 + 原始 SFT 行 + relation 端点列表），可直接交给
GPT 清洗代理或人工标注者。

## ④ 清洗输出格式

清洗结果每行一个 JSON：

```json
{"id": 0, "cleaned_assistant": "[ENTITY]\\n...\\n\\n[RELATION]\\n...\\n\\n[LOGIC_GROUP]\\n..."}
```

`id` 与清洗输入行的 `id` 一一对应，所有行都必须有结果。

可以用 `run_entity_v2_cleaning.py` 批量调用任意 OpenAI 兼容接口自动清洗：

```powershell
python 2stages\entity_v2_tools\run_entity_v2_cleaning.py `
  --input 2stages\data\entity_v2_clean\cleaning_inputs_style.jsonl `
  --out 2stages\data\entity_v2_clean\cleaned_answers.jsonl `
  --endpoint https://api.openai.com/v1 `
  --api-key <你的 key> `
  --model gpt-4o `
  --workers 1 --log-every 5
```

`--resume` 可跳过已完成行；`--limit`/`--start-index` 用于分批处理。
也可以不用脚本，逐行把 `prompt` 字段粘贴给 GPT，把返回的 cleaned assistant
存成同样格式。

## ⑤ 合并回原始格式

```powershell
python 2stages\entity_v2_tools\merge_cleaned_answers.py `
  --input data_raw\combined_train_sft_messages.jsonl `
  --cleaned 2stages\data\entity_v2_clean\cleaned_answers.jsonl `
  --out 2stages\data\entity_v2_clean\combined_train_entity_v2_clean_original.jsonl
```

## ⑥ 校验

```powershell
python 2stages\entity_v2_tools\validate_entity_v2_clean.py `
  --input 2stages\data\entity_v2_clean\combined_train_entity_v2_clean_original.jsonl `
  --out-json outputs\entity_v2_clean\validation_report.json `
  --out-md outputs\entity_v2_clean\validation_report.md
```

校验必须 `VALIDATION PASSED`（relation 端点全覆盖、类型合法、字段齐全）。
如果少数行的端点本来就是 mention 引用且清洗时允许保留，可加 `--allow-mention`
降级为 warning，但训练前建议尽量按 normalized_name 对齐。

## ⑦ 生成 SFT 训练数据

```powershell
python 2stages\build_two_stage_datasets.py `
  --train-input 2stages\data\entity_v2_clean\combined_train_entity_v2_clean_original.jsonl `
  --test-input 2stages\data\entity_v2_clean\combined_test_entity_v2_clean_original.jsonl `
  --train-entity-output 2stages\data\entity_v2_clean\combined_train_entity_v2_clean_sft_messages.jsonl `
  --train-relation-output 2stages\data\entity_v2_clean\combined_train_relation_v2_clean_sft_messages.jsonl `
  --test-entity-output 2stages\data\entity_v2_clean\combined_test_entity_v2_clean_sft_messages.jsonl `
  --test-relation-output 2stages\data\entity_v2_clean\combined_test_relation_v2_clean_sft_messages.jsonl `
  --entity-rules-file 2stages\entity_v2_prompt_addendum.md
```

## 约定

- 只改 assistant `[ENTITY]`；`[RELATION]`、`[LOGIC_GROUP]` 原样保留。
- 每个 relation source/target 必须出现在 entity `normalized_name` 中。
- 测试集只用于诊断实验；最终报告以原始测试集为准。
- `outputs/` 与 `2stages/data/entity_v2_clean/` 的中间产物不提交到 Git。
