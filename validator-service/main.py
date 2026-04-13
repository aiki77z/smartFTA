from typing import Any, Dict, List, Set

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import logging
from pathlib import Path

from dotenv import load_dotenv


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 加载同目录下的 .env，确保 DASHSCOPE_API_KEY / LLM_MODEL 等环境变量生效
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')


class GraphNode(BaseModel):
  id: str
  label: str | None = None
  type: str | None = None  # "top" | "intermediate" | "basic" | "gate" | "event"
  gate: str | None = None  # "AND" | "OR" | None
  # Allow passing rich info from frontend (usually in node.meta.event)
  event: Dict[str, Any] | None = None
  meta: Dict[str, Any] | None = None


class GraphEdge(BaseModel):
  id: str | None = None
  source: str
  target: str


class GraphPayload(BaseModel):
  nodes: List[GraphNode]
  edges: List[GraphEdge]


class ValidateRequest(BaseModel):
  graph: GraphPayload


app = FastAPI(title='FTA Validator Service')

origins = [
  'http://localhost:5173',
  'http://127.0.0.1:5173',
  'http://localhost:5174',
  'http://127.0.0.1:5174',
]

app.add_middleware(
  CORSMiddleware,
  allow_origins=origins,
  allow_credentials=True,
  allow_methods=['*'],
  allow_headers=['*'],
)


def _issue(
  level: str,
  code: str,
  message: str,
  node_ids: List[str] | None = None,
) -> Dict[str, Any]:
  return {
    'level': level,
    'code': code,
    'message': message,
    'node_ids': node_ids or [],
  }


def _empty_validation(msg: str) -> Dict[str, Any]:
  return {
    'validation': {
      'passed': True,
      'error_count': 0,
      'warning_count': 0,
      'info_count': 1,
      'issues': [
        _issue(
          level='INFO',
          code='EMPTY_GRAPH',
          message=msg,
          node_ids=[],
        )
      ],
    }
  }


@app.post('/validate-fault-tree')
def validate_fault_tree(payload: ValidateRequest) -> Dict[str, Any]:
  graph = payload.graph
  nodes_by_id: Dict[str, GraphNode] = {n.id: n for n in graph.nodes}

  if not nodes_by_id:
    return _empty_validation('图为空，未进行校验。')

  issues: List[Dict[str, Any]] = []

  # 顶事件检查
  top_nodes = [n for n in graph.nodes if (n.type or '').lower() == 'top']
  if len(top_nodes) == 0:
    issues.append(
      _issue(
        level='ERROR',
        code='NO_TOP_EVENT',
        message="未找到顶事件（type 为 'top' 的节点）。",
        node_ids=list(nodes_by_id.keys()),
      ),
    )
  elif len(top_nodes) > 1:
    issues.append(
      _issue(
        level='ERROR',
        code='MULTIPLE_TOP_EVENTS',
        message=f'检测到 {len(top_nodes)} 个顶事件，故障树应当只有一个顶事件。',
        node_ids=[n.id for n in top_nodes],
      ),
    )

  # 构建父子关系（当前前端边是 child -> parent）
  children_map: Dict[str, List[str]] = {nid: [] for nid in nodes_by_id}
  parents_map: Dict[str, List[str]] = {nid: [] for nid in nodes_by_id}

  for e in graph.edges:
    if e.source not in nodes_by_id or e.target not in nodes_by_id:
      continue
    child = e.source
    parent = e.target
    children_map[parent].append(child)
    parents_map[child].append(parent)

  # 基础结构规则
  # 基于图结构计算“事件子节点数量”和“是否存在逻辑门”
  event_children_count: Dict[str, int] = {nid: 0 for nid in nodes_by_id}
  for parent_id, child_ids in children_map.items():
    count = 0
    for cid in child_ids:
      node = nodes_by_id.get(cid)
      if not node:
        continue
      if (node.type or '').lower() != 'gate':
        count += 1
    event_children_count[parent_id] = count

  # 基于父子关系计算“该父节点是否有 gate 子节点”
  has_gate_for_event: Set[str] = set()
  for parent_id, child_ids in children_map.items():
    for cid in child_ids:
      node = nodes_by_id.get(cid)
      if node and (node.type or '').lower() == 'gate':
        has_gate_for_event.add(parent_id)
        break

  for nid, node in nodes_by_id.items():
    node_type = (node.type or '').lower()
    child_ids = children_map.get(nid, [])
    event_child_count = event_children_count.get(nid, 0)
    # has_gate：当前图结构下，该事件是否拥有 gate 子节点
    has_gate = nid in has_gate_for_event

    # 调试输出：关注所有作为“事件父节点”的非 basic / 非 gate 节点
    if node_type not in {'basic', 'gate'}:
      logger.info(
        "PARENT DEBUG id=%s label=%s type=%s children=%s event_child_count=%s has_gate=%s",
        nid,
        getattr(node, "label", None),
        node_type,
        child_ids,
        event_child_count,
        has_gate,
      )

    if node_type == 'basic' and child_ids:
      issues.append(
        _issue(
          level='ERROR',
          code='BASIC_HAS_CHILDREN',
          message=f"基本事件节点 '{node.label or nid}' 不应具有子节点。",
          node_ids=[nid, *child_ids],
        ),
      )

    # 叶子节点必须为基本事件：
    # 仅当中间事件既没有事件子节点、也没有逻辑门时，才认为它是“叶子中间事件”并报错
    if node_type == 'intermediate' and event_child_count == 0 and not has_gate:
      issues.append(
        _issue(
          level='ERROR',
          code='INTERMEDIATE_WITHOUT_CHILDREN',
          message=f"中间事件节点 '{node.label or nid}' 没有任何子事件，这违反“叶子节点必须为基本事件”的规则。",
          node_ids=[nid],
        ),
      )

    # 多个子事件子节点时必须有逻辑门 → 错误
    # 这里的“事件父节点”指所有非 basic / 非 gate 的节点（包括 top、intermediate 及其它可能的事件类型）
    if node_type not in {'basic', 'gate'} and event_child_count >= 2 and not has_gate:
      issues.append(
        _issue(
          level='ERROR',
          code='MULTI_CHILD_NO_GATE',
          message=f"节点 '{node.label or nid}' 有多个事件子节点，但未定义逻辑门（AND/OR），不符合故障树建模规范。",
          node_ids=[nid, *child_ids],
        ),
      )

    # 恰好 1 个事件子节点但没有逻辑门 → 提示缺少 gate（warning）
    if node_type not in {'basic', 'gate'} and event_child_count == 1 and not has_gate:
      issues.append(
        _issue(
          level='WARNING',
          code='MISSING_GATE',
          message=f"节点 '{node.label or nid}' 有子节点但未通过逻辑门（AND/OR）连接。",
          node_ids=[nid],
        ),
      )

  # 规则：检测“同一基本事件既通过中间事件间接连接顶事件，又直接作为顶事件子节点”的冗余路径
  # 更一般地：若存在边 A->C，且在忽略该直接边的情况下，沿父链仍然能从 A 到达 C，则认为存在冗余路径
  for child_id, parents in parents_map.items():
    for parent_id in parents:
      # 从 child_id 向上搜索，但在第一步忽略 parent_id 这条直接边
      queue = [p for p in parents if p != parent_id]
      visited_up: Set[str] = set(queue)
      found = False
      while queue and not found:
        current = queue.pop(0)
        if current == parent_id:
          found = True
          break
        for up in parents_map.get(current, []):
          if up not in visited_up:
            visited_up.add(up)
            queue.append(up)

      if found:
        child_node = nodes_by_id.get(child_id)
        parent_node = nodes_by_id.get(parent_id)
        issues.append(
          _issue(
            level='WARNING',
            code='REDUNDANT_PATH',
            message=(
              f"检测到冗余路径：基本事件或中间事件 '{child_node.label if child_node else child_id}' "
              f"既通过中间事件链路间接影响节点 '{parent_node.label if parent_node else parent_id}'，"
              f"又直接作为其子节点。该结构可能导致逻辑含义混淆或概率计算重复，建议梳理路径："
              f"如仅通过中间事件影响，则可移除直接连线；如确有“直接+间接”双重作用，应在命名和结构上明确区分。"
            ),
            node_ids=[child_id, parent_id],
          ),
        )

  # DFS 检测环和不可达节点
  visited: Set[str] = set()
  stack: Set[str] = set()
  cycle_nodes: Set[str] = set()

  def dfs(node_id: str) -> None:
    if node_id in stack:
      cycle_nodes.update(stack)
      return
    if node_id in visited:
      return
    visited.add(node_id)
    stack.add(node_id)
    for child_id in children_map.get(node_id, []):
      dfs(child_id)
    stack.remove(node_id)

  if len(top_nodes) == 1:
    dfs(top_nodes[0].id)
  else:
    roots = [nid for nid, parents in parents_map.items() if not parents]
    for r in roots:
      dfs(r)

  if cycle_nodes:
    issues.append(
      _issue(
        level='ERROR',
        code='CYCLE_DETECTED',
        message='检测到图中存在循环关联（故障树必须是无环结构）。',
        node_ids=sorted(cycle_nodes),
      ),
    )

  unreachable = [nid for nid in nodes_by_id if nid not in visited]
  if unreachable:
    issues.append(
      _issue(
        level='ERROR',
        code='DISCONNECTED_NODES',
        message='检测到未与顶事件连通的节点（可能是悬空子树或未连接事件）。',
        node_ids=unreachable,
      ),
    )

  # 检查：event 字段完整性（对齐后端 validator.py 的规则）
  required_event_fields = [
    'id',
    'name',
    'description',
    'errorLevel',
    'priority',
    'probability',
    'showProbability',
    'rules',
    'investigateMethod',
    'documents',
  ]

  def _resolve_event(node: GraphNode) -> Dict[str, Any] | None:
    # Prefer explicit node.event, fallback to node.meta.event (frontend usually stores it there)
    if isinstance(getattr(node, 'event', None), dict):
      return node.event
    meta = getattr(node, 'meta', None)
    if isinstance(meta, dict) and isinstance(meta.get('event'), dict):
      return meta.get('event')
    return None

  for nid, node in nodes_by_id.items():
    node_type = (node.type or '').lower()
    if node_type == 'gate':
      continue

    ev = _resolve_event(node)

    if node_type == 'top':
      # 顶事件 event 应为 null（允许缺省）
      if ev is not None:
        issues.append(
          _issue(
            level='WARNING',
            code='TOP_EVENT_HAS_EVENT',
            message='顶事件的event字段应为null',
            node_ids=[nid],
          ),
        )
      continue

    # intermediate/basic（以及其它非 gate 非 top 的事件节点）必须有 event
    if ev is None:
      issues.append(
        _issue(
          level='ERROR',
          code='MISSING_EVENT',
          message='中间事件/底事件必须有event对象',
          node_ids=[nid],
        ),
      )
      continue

    for field in required_event_fields:
      if field not in ev:
        issues.append(
          _issue(
            level='ERROR',
            code='MISSING_EVENT_FIELD',
            message=f'event对象缺少字段：{field}',
            node_ids=[nid],
          ),
        )

    # errorLevel 为空时给 INFO 提示
    err_level = ev.get('errorLevel', '')
    if not isinstance(err_level, str) or not err_level.strip():
      issues.append(
        _issue(
          level='INFO',
          code='NO_ERROR_LEVEL',
          message='没有故障等级（errorLevel为空）',
          node_ids=[nid],
        ),
      )

    # documents 为空时给 INFO 提示
    if not ev.get('documents'):
      issues.append(
        _issue(
          level='INFO',
          code='NO_DOCUMENTS',
          message='没有溯源文档（documents为空）',
          node_ids=[nid],
        ),
      )

  error_count = sum(1 for i in issues if i['level'] == 'ERROR')
  warning_count = sum(1 for i in issues if i['level'] == 'WARNING')
  info_count = sum(1 for i in issues if i['level'] == 'INFO')

  passed = error_count == 0

  return {
    'validation': {
      'passed': passed,
      'error_count': error_count,
      'warning_count': warning_count,
      'info_count': info_count,
      'issues': issues,
    }
  }


# ===== 高保真图片导出（Headless 浏览器截图） =====

class ExportImageRequest(BaseModel):
  url: str
  selector: str | None = None
  width: int | None = 1600
  height: int | None = 900
  scale: float | None = 3.0
  theme: str | None = None  # 'light' or 'dark'


@app.post('/export-fault-tree-image')
async def export_fault_tree_image(req: ExportImageRequest) -> Response:
  """
  使用 Headless 浏览器对前端页面进行截图，保留前端真实渲染效果（包括字体）。

  依赖：
  - pip install playwright
  - playwright install chromium
  """
  try:
    from playwright.async_api import async_playwright
  except ImportError:
    raise RuntimeError(
      'playwright 未安装，请先在 validator-service 虚拟环境中执行：'
      'pip install playwright && playwright install chromium',
    )

  async with async_playwright() as p:
    browser = await p.chromium.launch()
    # 使用 device_scale_factor 实现更高分辨率（近似 3x）
    context = await browser.new_context(
      viewport={'width': req.width or 1600, 'height': req.height or 900},
      device_scale_factor=req.scale or 3.0,
    )
    page = await context.new_page()

    # 先加载页面并记录 HTTP 状态，避免 431 等错误导致页面未渲染。
    # 这里尽量给足时间，React + 资源加载仍可能较慢。
    goto_resp = await page.goto(req.url, wait_until='networkidle', timeout=30000)
    if goto_resp is not None and goto_resp.status >= 400:
      raise RuntimeError(f'访问 {req.url} 失败，HTTP {goto_resp.status}')

    # 根据请求主题强制设置前端模式（保持与当前 Web 一致），
    # 直接修改根元素的 class，避免依赖按钮文案或默认状态。
    if req.theme in {'light', 'dark'}:
      try:
        await page.evaluate(
          """(desired) => {
            const root = document.querySelector('.fta-layout');
            if (!root) return;
            const cls = 'fta-layout--dark';
            const needDark = desired === 'dark';
            if (needDark) {
              if (!root.classList.contains(cls)) {
                root.classList.add(cls);
              }
            } else {
              root.classList.remove(cls);
            }
          }""",
          req.theme,
        )
        await page.wait_for_timeout(200)
      except Exception:
        pass

    # 隐藏画布上的交互控件（缩放按钮、缩略图、图例按钮、自动调整视图按钮等）
    # 以及 AI 建议浮窗，但保留页面其它结构（例如顶部 header、左侧 JSON 面板等）
    await page.add_style_tag(
      content="""
      .react-flow__controls,
      .react-flow__minimap,
      .fta-legend-toggle,
      .fta-fitview-btn,
      .fta-ai-panel,
      .fta-ai-minimized {
        display: none !important;
      }
      """,
    )

    # 默认只截取故障树画布容器（.fta-canvas-wrapper），并隐藏内部交互控件
    target_selector = req.selector or '.fta-canvas-wrapper'
    # React 页面是前端渲染，wait_until='networkidle' 不能保证目标节点已挂载完成。
    # 这里显式等待容器出现，避免“未找到选择器”导致截图失败。
    try:
      await page.wait_for_selector(target_selector, timeout=30000)
    except Exception as exc:
      await context.close()
      await browser.close()
      raise RuntimeError(
        f'未找到选择器 {target_selector} 对应的元素（url={req.url}）: {exc}'
      ) from exc

    el = await page.query_selector(target_selector)
    if not el:
      await context.close()
      await browser.close()
      raise RuntimeError(f'未找到选择器 {target_selector} 对应的元素')

    # 使用 device scale 截图，保证清晰度，与 device_scale_factor 对齐
    content_png = await el.screenshot(type='png', scale='device')

    await context.close()
    await browser.close()

  return Response(content_png, media_type='image/png')

