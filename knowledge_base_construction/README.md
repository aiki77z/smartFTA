# 知识抽取流水线文档

## 概述

本工具集提供了一套完整的 PDF 文档知识抽取流水线，能够将技术文档（如数控系统维修手册）转换为结构化数据，包括：

1. **PDF → Markdown**：使用 MinerU 将 PDF 转换为 Markdown 格式
2. **Markdown 分块**：基于标题层级和表格保护将文档切分为逻辑块
3. **实体提取**：利用大语言模型识别文档中的专业实体（设备、故障、参数等）
4. **关系提取**：识别实体之间的语义关系（组成、触发、控制等）

最终输出 JSON 和 CSV 格式的实体及关系数据，便于下游知识图谱构建或检索增强生成（RAG）。

---

## 文件结构

```
.
├── run.py                      # 主流水线脚本（一键执行全流程）
├── trans_file_to_md.py         # PDF → Markdown 转换（调用 MinerU）
├── chunk_md.py                 # Markdown 文档分块
├── extract_entities.py         # 实体提取与合并
├── extract_relations.py        # 关系提取与 CSV 导出
├── generate_prompt_relation.py # 实体/关系提取的提示词模板
├── llm_caller_relation.py      # 大语言模型调用封装（OpenAI 兼容）
└── README.md                   # 本文档
```

---

## 环境依赖

### 基础环境
- Python 3.8+
- 安装依赖包：
```bash
pip install openai
```

### MinerU 工具
本流水线依赖 [MinerU](https://github.com/opendatalab/MinerU) 进行 PDF 转换，请按官方文档安装：
```bash
pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple
pip install uv -i https://mirrors.aliyun.com/pypi/simple
uv pip install -U "mineru[all]" -i https://mirrors.aliyun.com/pypi/simple
# 参考 https://github.com/opendatalab/MinerU
```

### 大语言模型 API
需要提供一个 OpenAI 兼容的 API 接口（如阿里云百炼、DeepSeek、OpenAI 官方等）。在 `llm_caller_relation.py` 中配置：

```python
OPENAI_API_KEY = "your-api-key"
OPENAI_BASE_URL = "https://your-api-endpoint/v1"
OPENAI_MODEL_NAME = "your-model-name"
```

也可通过环境变量设置：
```bash
export OPENAI_API_KEY="sk-xxx"
export OPENAI_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export OPENAI_MODEL_NAME="qwen3.5-plus"
```

---

## 模块说明

### 1. trans_file_to_md.py – PDF 转 Markdown

**功能**：调用 MinerU 命令行工具将 PDF 转换为 Markdown 文件。

**用法**：
```bash
python trans_file_to_md.py -i input.pdf -o ./output -b pipeline -m ocr
```
- `-i`：输入 PDF 路径
- `-o`：输出目录
- `-b`：后端模式（默认 `pipeline`）
- `-m`：运行模式（默认 `ocr`）

**注意**：MinerU 会在输出目录下生成与 PDF 同名的子文件夹，包含 `.md` 文件。

---

### 2. chunk_md.py – Markdown 分块

**功能**：解析 Markdown 文档，按标题层级切分，并保护表格完整性。

**特性**：
- 识别 `#` 标题层级（支持 1~6 级）
- 表格整体保留，不跨块拆分
- 超长段落按 `chunk_size` 二次分割
- 提取块中的首张图片路径（`image_path` 字段）

**输出 JSON 结构**（每个块）：
```json
{
  "id": 0,
  "chunk_name": "文档名",
  "content": "文本内容",
  "chapter": "章标题",
  "section": "节标题",
  "subsection": "小节标题",
  "section_path": "1.2.3",
  "source": 起始行号,
  "file": "源文件名",
  "image_path": "图片路径（可选）",
  "table": "表格内容（仅表格块）"
}
```

**用法**：
```bash
python chunk_md.py --input doc.md --output chunks.json --chunk_size 800
```

---

### 3. extract_entities.py – 实体提取与合并

**功能**：
- 对每个分块调用 LLM 提取实体（名称 + 类别）
- 过滤无效实体（不在原文中出现、长度 >20 字符）
- 支持增量处理（已处理块跳过）
- 合并所有实体，记录每个实体出现的 `chunk_id`

**实体类别**（定义在提示词中）：
- 技术系统与设备
- 故障现象与报警
- 硬件组件与元器件
- 技术概念与方法
- 参数与数据
- 工具与仪器

**输出文件**：
- `entities.jsonl`：每行一个块的实体提取结果
- `entities_merged.json`：全局合并实体列表

**用法**：
```bash
python extract_entities.py \
  --input chunks.json \
  --output-entities entities.jsonl \
  --output-merged entities_merged.json \
  [--print-raw-text]
```

---

### 4. extract_relations.py – 关系提取

**功能**：
- 基于已提取的实体，对每个分块调用 LLM 识别实体间的关系
- 支持 12 种预定义关系类型（组成、控制操作、故障触发、测试测量、参数配置、状态变化、通讯连接、故障处理、功能支持、时序关系、依赖关系、比较关系）
- 关系以 `<实体1, 关系类型, 具体动词, 实体2>` 格式输出
- 输出 JSONL 和 CSV 文件

**输出文件**：
- `relations.jsonl`：每行一个块的关系提取结果
- `relations.csv`：所有关系的扁平化表格

**用法**：
```bash
python extract_relations.py \
  --input-chunks entities.jsonl \
  --input-entities entities_merged.json \
  --output-relations relations.jsonl \
  --output-csv relations.csv
```

---

### 5. generate_prompt_relation.py – 提示词模板

**功能**：为实体提取和关系提取生成动态提示词。

- `generate_entity_prompt_and_context(chunk_name, content)`  
  返回实体提取的 system prompt 和 user prompt
- `generate_relation_prompt_and_context_second(chunk_name, content, entities)`  
  返回关系提取的 system prompt 和 user prompt

提示词中包含领域约束、禁止抽取的泛化词汇、实体类型定义、关系框架和示例。

---

### 6. llm_caller_relation.py – LLM 调用封装

**功能**：统一调用 OpenAI 兼容的大模型 API。

**配置**（直接修改文件或环境变量）：
```python
OPENAI_API_KEY = "sk-xxx"
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_MODEL_NAME = "gpt-3.5-turbo"
LLM_TYPE = "openai"
MAX_TOKENS = 2048
TEMPERATURE = 0.1
REQUEST_TIMEOUT = 300
```

**函数**：
```python
call_llm(prompt, context, mode="entity") -> str
```
- `prompt`：用户提示词
- `context`：系统上下文（角色设定）
- `mode`：预留参数，未使用

支持重试（最多 3 次）。

---

### 7. run.py – 完整流水线

**功能**：串联上述所有步骤，一键执行。

**用法**：
```bash
python run.py --pdf input.pdf --output-dir ./output --chunk-size 800
```

**参数**：
| 参数                  | 说明                                |
| --------------------- | ----------------------------------- |
| `--pdf` / `-p`        | 输入 PDF 文件路径（必选）           |
| `--output-dir` / `-o` | 输出根目录，默认 `./output`         |
| `--chunk-size` / `-s` | 分块大小（字符数），默认 800        |
| `--skip-mineru`       | 跳过 PDF 转换，直接使用已有 MD 文件 |
| `--skip-entity`       | 跳过实体提取（只执行分块）          |
| `--skip-relation`     | 跳过关系统取（只执行到实体合并）    |
| `--print-raw-text`    | 打印 LLM 原始返回（调试用）         |

**运行示例**：
```bash
python run.py -p manual.pdf -o results -s 1000
```

**输出文件**（位于 `--output-dir`）：
- `{pdf_stem}_chunks.json` – 分块结果
- `{pdf_stem}_entities.jsonl` – 逐块实体
- `{pdf_stem}_entities_merged.json` – 合并实体
- `{pdf_stem}_relations.jsonl` – 逐块关系
- `{pdf_stem}_relations.csv` – 关系 CSV

---

## 典型工作流示例

### 仅分块（不提取实体）
```bash
python run.py -p doc.pdf -o ./out --skip-entity
```

### 仅提取实体（已有分块文件）
```bash
python extract_entities.py -i chunks.json -oe entities.jsonl -om merged.json
```

### 仅提取关系（已有实体）
```bash
python extract_relations.py -ic entities.jsonl -ie merged.json -or rel.jsonl -oc rel.csv
```

---

## 自定义与扩展

### 修改实体类别
编辑 `generate_prompt_relation.py` 中的 `entity_category_definition` 字典。

### 修改关系类型
编辑 `generate_prompt_relation.py` 中的 `predefined_relations` 多行字符串，添加或删除关系定义。

### 调整 LLM 参数
修改 `llm_caller_relation.py` 中的 `MAX_TOKENS`、`TEMPERATURE` 等常量。

### 适配其他大模型
`call_llm` 函数目前仅支持 OpenAI 兼容接口。如需其他 API（如 Claude、Gemini），可扩展 `call_llm` 中的分支。

---

## 注意事项

1. **PDF 转换**：MinerU 可能需要 GPU 或特定系统依赖，请确保安装正确。首次运行会自动下载模型文件。
2. **API 费用**：实体和关系提取会消耗大量 token，建议使用便宜模型（如 `qwen-turbo`）进行测试。
3. **编码问题**：`run.py` 已强制使用 UTF-8 编码，避免 Windows 下 GBK 报错。
4. **表格保护**：Markdown 表格和 HTML 表格均会被整体保留，不会被截断。
5. **增量处理**：实体和关系提取支持断点续跑，已处理的 `chunk_id` 会跳过。

---

## 常见问题

**Q: MinerU 转换后找不到 .md 文件？**  
A: 检查输出目录，MinerU 通常会在 `output_dir/pdf_name/` 下生成。`run.py` 会递归搜索。

**Q: LLM 返回格式解析失败怎么办？**  
A: 使用 `--print-raw-text` 查看原始输出，调整提示词中的示例或放宽解析正则。

**Q: 实体提取结果为空？**  
A: 检查 chunk 内容是否包含专业技术词汇；尝试降低 `TEMPERATURE` 或更换模型。

**Q: 如何只处理一个 Markdown 文件（不经过 PDF 转换）？**  
A: 直接调用 `chunk_md.py`，然后可选调用实体/关系提取脚本，不需要 `run.py`。

---
