from typing import Any, Dict, List, Optional
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


router = APIRouter()
logger = logging.getLogger(__name__)


class GraphData(BaseModel):
  nodes: List[Dict[str, Any]]
  edges: List[Dict[str, Any]]


class ValidationIssue(BaseModel):
  code: Optional[str] = None
  level: Optional[str] = None
  message: Optional[str] = None
  node_ids: Optional[List[str]] = None


class ValidationResult(BaseModel):
  passed: Optional[bool] = None
  error_count: Optional[int] = 0
  warning_count: Optional[int] = 0
  info_count: Optional[int] = 0
  issues: Optional[List[ValidationIssue]] = None


class AiValidateRequest(BaseModel):
  tree_json: Optional[Dict[str, Any]] = None
  graph: GraphData
  validation: Optional[ValidationResult] = None


class AiValidateResponse(BaseModel):
  summary: Optional[str] = None
  suggestions: Optional[str] = None
  issues: Optional[List[str]] = None
  raw_model_output: Optional[Any] = None


# 固定 DashScope 兼容 OpenAI 模式的 base URL，key 和 model 每次从环境变量读取，避免导入时机问题
DASHSCOPE_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'


def _build_prompt(payload: AiValidateRequest) -> str:
  tree_json_part = payload.tree_json or {}
  validation_part = payload.validation.dict() if payload.validation else {}

  prompt = f"""
你是一名工业设备可靠性工程师兼故障树分析专家，任务是：
- 阅读当前故障树结构与事件信息
- 结合已有的逻辑校验结果（来自规则引擎）
- 用中文给出简洁、严格、专业的“AI 校验结论”和“优化建议”。

【故障树 graph 数据（节点/连线）】:
{payload.graph.model_dump_json(indent=2, ensure_ascii=False)}

【原始故障树 JSON（如有）】:
{tree_json_part}

【已有逻辑校验结果】:
{validation_part}

请输出一段简短清晰的中文点评，包含以下部分：
1. 整体评价：当前故障树在结构完整性、逻辑合理性上的总体判断；
2. 关键问题：直接分条列出可能存在的最重要的建模问题（例如：顶事件定义不清、逻辑门类型不合理、事件颗粒度不一致、关键基本事件缺少描述/概率/溯源等），如果你觉得没有很重要，就无需提出这个问题；
3. 具体优化建议：针对每类问题给出可操作的修改建议（如何增删节点、调整逻辑门、补充信息等），尽量简洁，符合人的行为逻辑；
4. 如有需要，可提出对专家的补充信息需求。

请直接返回一段分点文本，不需要有其他多余的格式，总字数不超过500字，语气专业但易于理解。
"""
  return prompt.strip()


async def _call_qwen(prompt: str) -> Dict[str, Any]:
  # 每次调用时从环境读取，确保 .env 在 main.py 中 load_dotenv 之后也能生效
  api_key = os.getenv('DASHSCOPE_API_KEY', 'YOUR_DASHSCOPE_API_KEY')
  model_name = os.getenv('LLM_MODEL', 'qwen-plus')

  # 基本环境检查日志（不打印具体密钥，以免泄露）
  logger.info(
    'AI validate: using model=%s, base_url=%s, key_configured=%s',
    model_name,
    DASHSCOPE_BASE_URL,
    bool(api_key and api_key != 'YOUR_DASHSCOPE_API_KEY'),
  )

  if not api_key or api_key == 'YOUR_DASHSCOPE_API_KEY':
    logger.error('AI validate: DASHSCOPE_API_KEY 未配置或仍为默认占位值')
    raise HTTPException(status_code=500, detail='DASHSCOPE_API_KEY 未配置')

  headers = {
    'Authorization': f'Bearer {api_key}',
    'Content-Type': 'application/json',
  }

  body = {
    'model': model_name,
    'messages': [
      {'role': 'system', 'content': '你是一名熟悉工业设备与故障树分析的专家。'},
      {'role': 'user', 'content': prompt},
    ],
    'temperature': 0.3,
  }

  async with httpx.AsyncClient(base_url=DASHSCOPE_BASE_URL, timeout=60.0, trust_env=False) as client:
    try:
      resp = await client.post('/chat/completions', headers=headers, json=body)
    except Exception as exc:
      logger.exception('AI validate: 请求 DashScope 失败: %s', exc)
      raise HTTPException(status_code=500, detail=f'DashScope 请求异常: {exc}') from exc

    logger.info(
      'AI validate: DashScope response status=%s, text_prefix=%s',
      resp.status_code,
      resp.text[:300],
    )

    if resp.status_code != 200:
      raise HTTPException(
        status_code=500,
        detail=f'DashScope LLM error: {resp.status_code} {resp.text}',
      )
    return resp.json()


@router.post('/ai-validate-fault-tree', response_model=AiValidateResponse)
async def ai_validate_fault_tree(payload: AiValidateRequest) -> AiValidateResponse:
  """
  与前端 FaultTreePage 对接的 AI 校验端点。
  """
  try:
    logger.info(
      'AI validate: request received, nodes=%d, edges=%d, has_validation=%s',
      len(payload.graph.nodes),
      len(payload.graph.edges),
      payload.validation is not None,
    )

    prompt = _build_prompt(payload)
    logger.info('AI validate: prompt length=%d', len(prompt))

    data = await _call_qwen(prompt)

    content = (
      data.get('choices', [{}])[0]
      .get('message', {})
      .get('content', '')
    ).strip()

    logger.info('AI validate: model content length=%d', len(content))

    if not content:
      raise HTTPException(status_code=500, detail='LLM 返回内容为空')

    parts = content.split('\n\n', 1)
    first_paragraph = parts[0]
    suggestions_text = content

    return AiValidateResponse(
      summary=f'AI 总体评价（自动生成）：\n{first_paragraph}',
      suggestions=suggestions_text,
      issues=[],
      raw_model_output=None,
    )
  except HTTPException as http_exc:
    # 记录详细错误方便前端反馈
    logger.error('AI validate: HTTPException detail=%s', http_exc.detail)
    raise
  except Exception as exc:
    logger.exception('AI validate: 未处理异常: %s', exc)
    raise HTTPException(status_code=500, detail=f'AI 校验失败: {exc}') from exc

