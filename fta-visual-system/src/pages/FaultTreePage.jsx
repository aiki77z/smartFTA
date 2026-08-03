import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { getWorkspace, markProjectReviewed } from '../utils/projectStore.js'
import {
  upsertCanvasDraft,
  inferCanvasDisplayTitle,
  readCanvasDraftMap,
} from '../utils/canvasDraftStore.js'
import { loadExploded3dBundle } from '../utils/exploded3dProjectStore.js'
import FaultTreeCanvas from '../components/fta/FaultTreeCanvas.jsx'
import rawFtaSample from '../raw-FTA/raw-FTA-new.json'
import ExplodedViewer from '../components/ExplodedViewer.jsx'
import {
  getTree,
  getTreeHistory,
  getTreeVersion,
  generateTree,
  getFtaBackendBaseUrl,
  getBatchJobItem,
  pollGenerationJobItem,
  saveTree,
  deleteTree,
  validateFaultTreeGraph,
  validateTreeSemantic,
  getFtaChunk,
} from '../api/ftaBackend.js'
import { editFaultTreeWithAi, sendAssistantAgentMessage, truncateAssistantSession } from '../api/ftaAiEditor.js'
import {
  parseRawFtaJson,
  parseTreeDataJson,
  formatTriggerRuleNaturalLanguage,
} from '../utils/ftaParser.js'
import {
  IconChevronLeft,
  IconClose,
  IconBraces,
  IconCube,
  IconMinus,
  IconRedo,
  IconUndo,
} from '../components/icons.jsx'
import { useTheme } from '../components/ThemeProvider.jsx'
import ThemeToggle from '../components/ThemeToggle.jsx'
import SaveDescriptionModal from '../components/SaveDescriptionModal.jsx'
import VersionSelect from '../components/VersionSelect.jsx'
import TaskProgressHistoryModal from '../components/TaskProgressHistoryModal.jsx'
import {
  addChildNode,
  addChildUnderGate,
  deleteNode,
  deleteGate,
  changeGateType,
  renameNode,
  changeEventType,
  insertGate,
  insertParentEvent,
  duplicateEventNode,
  connectNodes,
  deleteEdgeById,
  graphToRawJson,
  graphToTreeDataJson,
  getNodeEditInfo,
  changeEventDescription,
  patchEventFields,
} from '../utils/ftaGraphEditor.js'
import '../styles/fta.css'
import '../styles/fta-error-level.css'
import '../styles/fta-probability.css'

const TYPE_LABELS = { top: '顶事件', intermediate: '中间事件', basic: '底事件' }
const EVENT_TYPES = ['top', 'intermediate', 'basic']
// 当快照 JSON 文本过长时，/fta-viewer?snapshot=... 可能触发 Vite 431（Request Header Fields Too Large）
// 这里先用“文本长度”做前置限制，避免高保真导出直接失败。
const MAX_HIRES_SNAPSHOT_JSON_LEN = 7000
const AI_ASSIST_STORE_PREFIX = 'fta-ai-assistant:'
const AI_ASSIST_FILE_PREFIX = 'fta-ai-selected-files:'
const ENABLE_MOCK_IMPORTED_KB_FILE = true
const MOCK_IMPORTED_KB_FILE = {
  id: 'mock-huawei-luna2000-alarm-reference',
  name: 'HUAWEI LUNA2000 alarm reference (KB v2 imported)',
  size: 0,
  status: 'done',
  uploadProgress: 100,
  parseProgress: 100,
  kbImportComplete: true,
  kbCategory: 'document',
  kbCategoryLabel: 'KB v2 imported',
  sourceType: 'manual_document',
  fileId: 'huawei_luna2000_alarm_reference',
  fileVersionId: 'huawei_luna2000_alarm_reference_v1',
  versionNo: 1,
  isActive: true,
  importedFileName: 'HUAWEI LUNA2000 alarm reference',
  createdAt: Date.now(),
  updatedAt: Date.now(),
}

function ensureMockImportedKbFile(files) {
  const list = Array.isArray(files) ? files : []
  if (!ENABLE_MOCK_IMPORTED_KB_FILE) return list
  const exists = list.some(
    (f) =>
      f?.id === MOCK_IMPORTED_KB_FILE.id ||
      String(f?.fileVersionId || '') === MOCK_IMPORTED_KB_FILE.fileVersionId,
  )
  return exists ? list : [MOCK_IMPORTED_KB_FILE, ...list]
}

function makeChunkRefKey(ref) {
  if (ref && typeof ref === 'object') {
    const chunkId = ref.chunk_id ?? ref.chunkId ?? ref.id
    const fileVersionId = ref.file_version_id ?? ref.fileVersionId ?? ''
    if (chunkId === undefined || chunkId === null || chunkId === '') return ''
    return fileVersionId ? `${fileVersionId}::${chunkId}` : String(chunkId)
  }
  if (ref === undefined || ref === null || ref === '') return ''
  return String(ref)
}

function renderAssistantInlineMarkdown(text, keyPrefix) {
  const source = String(text || '')
  const parts = []
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g
  let last = 0
  let index = 0
  for (const match of source.matchAll(pattern)) {
    if (match.index > last) {
      parts.push(source.slice(last, match.index))
    }
    const token = match[0]
    if (token.startsWith('**')) {
      parts.push(
        <strong key={`${keyPrefix}-strong-${index}`}>
          {token.slice(2, -2)}
        </strong>,
      )
    } else {
      parts.push(
        <code key={`${keyPrefix}-code-${index}`}>
          {token.slice(1, -1)}
        </code>,
      )
    }
    last = match.index + token.length
    index += 1
  }
  if (last < source.length) {
    parts.push(source.slice(last))
  }
  return parts
}

function AssistantMarkdownMessage({ content }) {
  const lines = String(content || '').split(/\r?\n/)
  const blocks = []
  let paragraph = []
  let listItems = []
  let listType = 'ul'

  const flushParagraph = () => {
    if (!paragraph.length) return
    const text = paragraph.join('\n')
    const key = `p-${blocks.length}`
    blocks.push(
      <p key={key}>
        {renderAssistantInlineMarkdown(text, key)}
      </p>,
    )
    paragraph = []
  }

  const flushList = () => {
    if (!listItems.length) return
    const key = `list-${blocks.length}`
    const Tag = listType
    blocks.push(
      <Tag key={key}>
        {listItems.map((item, idx) => (
          <li key={`${key}-${idx}`}>
            {renderAssistantInlineMarkdown(item, `${key}-${idx}`)}
          </li>
        ))}
      </Tag>,
    )
    listItems = []
  }

  lines.forEach((line) => {
    const trimmed = line.trim()
    if (!trimmed) {
      flushParagraph()
      flushList()
      return
    }

    const unordered = trimmed.match(/^[-*]\s+(.+)$/)
    const ordered = trimmed.match(/^\d+[.)]\s+(.+)$/)
    if (unordered || ordered) {
      flushParagraph()
      const nextType = unordered ? 'ul' : 'ol'
      if (listItems.length && listType !== nextType) {
        flushList()
      }
      listType = nextType
      listItems.push((unordered || ordered)[1])
      return
    }

    flushList()
    paragraph.push(trimmed)
  })

  flushParagraph()
  flushList()

  return <div className="fta-assistant-markdown">{blocks}</div>
}

/** 说明后端约束：当前 FTA-GNR 在排队生成前会校验顶事件必须在选源内图谱/目录可解析（见 _ensure_catalog_entry），非前端列表导致 */
function formatGenerateBackendError(raw) {
  const s = String(raw || '')
  if (s.includes('当前选源范围内未找到顶事件')) {
    return `${s}\n\n说明：这是后端当前规则——解析出的顶事件须在「当前选源」对应的 Neo4j/目录中存在，才会进入生成队列。若需要「任意顶事件名都可生成」，需在后端放宽或改造该校验（例如允许无图谱匹配时走纯 LLM 草稿模式）。`
  }
  return s
}

/** 将 /api/tree/generate 同步返回（含 tree_data）转为 applyBackendVersionPayload 所需结构 */
function buildVersionPayloadFromGenerateResponse(resp) {
  const td = resp?.tree_data ?? resp?.treeData
  if (!td || typeof td !== 'object') return null
  const v = Number(resp?.version)
  const topName = resp?.parsed_prompt?.catalog_top_event || resp?.parsed_prompt?.top_event || ''
  return {
    version: Number.isFinite(v) && v >= 1 ? v : 1,
    tree_data: td,
    editor: 'AI',
    created_at: new Date().toISOString(),
    description: topName ? `AI 初始生成：${topName}` : 'AI 初始生成',
    is_ai_generated: true,
  }
}

/**
 * 兼容后端更新后的返回结构：
 * - 可能包裹在 {data}/{result}/{payload} 内
 * - 可能使用 camelCase 字段（treeId/treeData/jobId/itemId/parsedPrompt/...）
 */
function normalizeGenerateResponse(raw) {
  if (!raw || typeof raw !== 'object') return raw
  const unwrapped = raw?.data || raw?.result || raw?.payload || raw
  if (!unwrapped || typeof unwrapped !== 'object') return unwrapped

  const mapped = { ...unwrapped }
  if (mapped.treeId && !mapped.tree_id) mapped.tree_id = mapped.treeId
  if (mapped.treeData && !mapped.tree_data) mapped.tree_data = mapped.treeData
  if (mapped.jobId && !mapped.job_id) mapped.job_id = mapped.jobId
  if (mapped.itemId && !mapped.item_id) mapped.item_id = mapped.itemId
  if (mapped.parsedPrompt && !mapped.parsed_prompt) mapped.parsed_prompt = mapped.parsedPrompt
  if (mapped.selectedFileVersionIds && !mapped.selected_file_version_ids) {
    mapped.selected_file_version_ids = mapped.selectedFileVersionIds
  }
  if (mapped.mode && typeof mapped.mode === 'string') mapped.mode = String(mapped.mode)
  return mapped
}

/** 与后端 job-item.events 的 seq 错开（本地 tick / 提示用 1e6+） */
const ASSISTANT_LOCAL_EVENT_SEQ_MIN = 1_000_000

function mapJobItemEventsToTaskFormat(item) {
  const evs = Array.isArray(item?.events) ? item.events : []
  return evs
    .map((e, idx) => ({
      seq: Number(e.seq) || idx + 1,
      ts: e.ts || e.created_at || new Date().toISOString(),
      agent: e.agent || 'Agent',
      avatar: String((e.agent || 'A')).slice(0, 1),
      level: String(e.level || 'INFO').toUpperCase(),
      text: String(e.text || ''),
      progress: e.progress,
      stage: e.stage || '',
    }))
    .sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0))
}

/** 每轮轮询用后端返回的完整 events 覆盖服务端段，避免增量 seq 与 Mongo $slice 截断错位导致进度停更 */
function mergeBackendJobEventsIntoTaskRef(ref, taskId, item, bumpRevision) {
  const mapped = mapJobItemEventsToTaskFormat(item)
  if (!mapped.length) return
  const prev = ref.current.get(taskId) || []
  const locals = prev.filter((e) => (Number(e.seq) || 0) >= ASSISTANT_LOCAL_EVENT_SEQ_MIN)
  ref.current.set(taskId, [...locals, ...mapped])
  bumpRevision?.()
}

function safeJsonParse(raw, fallback) {
  try {
    return JSON.parse(raw)
  } catch {
    return fallback
  }
}

function readAiAssistStore(key) {
  if (typeof window === 'undefined') return null
  try {
    const raw = localStorage.getItem(`${AI_ASSIST_STORE_PREFIX}${key}`)
    if (!raw) return null
    const parsed = safeJsonParse(raw, null)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

function writeAiAssistStore(key, payload) {
  if (typeof window === 'undefined') return
  try {
    localStorage.setItem(`${AI_ASSIST_STORE_PREFIX}${key}`, JSON.stringify(payload || {}))
  } catch {
    // ignore quota errors
  }
}

function readSelectedFiles(key) {
  if (typeof window === 'undefined') return []
  try {
    const raw = localStorage.getItem(`${AI_ASSIST_FILE_PREFIX}${key}`)
    if (!raw) return []
    const parsed = safeJsonParse(raw, null)
    if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean)
    // backward compatible: previous single string
    if (typeof parsed === 'string') return parsed ? [parsed] : []
    if (typeof raw === 'string') return raw ? [raw] : []
    return []
  } catch {
    return []
  }
}

function writeSelectedFiles(key, values) {
  if (typeof window === 'undefined') return
  try {
    localStorage.setItem(`${AI_ASSIST_FILE_PREFIX}${key}`, JSON.stringify(values || []))
  } catch {
    // ignore
  }
}

/** 比较节点内容（忽略 AI 差异标记与 raw，避免误报） */
function metaForStructuralCompare(meta) {
  if (!meta || typeof meta !== 'object') return {}
  const copy = { ...meta }
  delete copy._diffStatus
  delete copy._diffGhost
  delete copy.raw
  return copy
}

function nodeStructurallyChanged(prev, next) {
  if (!prev || !next) return true
  if (String(prev.id) !== String(next.id)) return true
  if (prev.label !== next.label) return true
  if (prev.type !== next.type) return true
  try {
    const a = JSON.stringify(metaForStructuralCompare(prev.meta))
    const b = JSON.stringify(metaForStructuralCompare(next.meta))
    return a !== b
  } catch {
    return true
  }
}

function computeGraphDiff(prevGraph, nextGraph) {
  const prevNodes = new Map((prevGraph?.nodes || []).map((n) => [String(n.id), n]))
  const nextNodes = new Map((nextGraph?.nodes || []).map((n) => [String(n.id), n]))
  const added = []
  const modified = []
  const removed = []

  nextNodes.forEach((n, id) => {
    const prev = prevNodes.get(id)
    if (!prev) {
      added.push(id)
      return
    }
    if (nodeStructurallyChanged(prev, n)) modified.push(id)
  })
  prevNodes.forEach((_n, id) => {
    if (!nextNodes.has(id)) removed.push(id)
  })
  return { added, modified, removed }
}

function edgeMergeKey(e) {
  return `${String(e.source)}|${String(e.target)}`
}

/** next 覆盖同键；保留 prev 中仅与「待删」子图相关的边，使删除节点在 Accept 前仍连在原结构 */
function mergeEdgesForPendingEdit(prevGraph, nextGraph) {
  const map = new Map()
  for (const e of nextGraph?.edges || []) {
    map.set(edgeMergeKey(e), { ...e })
  }
  for (const e of prevGraph?.edges || []) {
    const k = edgeMergeKey(e)
    if (!map.has(k)) map.set(k, { ...e })
  }
  return [...map.values()]
}

function buildPendingAiEditGraph(prevGraph, nextGraph, diff) {
  const prevNodes = new Map((prevGraph?.nodes || []).map((n) => [String(n.id), n]))
  const addedSet = new Set(diff?.added || [])
  const modifiedSet = new Set(diff?.modified || [])
  const removedSet = new Set(diff?.removed || [])

  const mergedNodes = []
  for (const n of nextGraph?.nodes || []) {
    const id = String(n.id)
    const nextMeta = { ...(n.meta || {}) }
    if (addedSet.has(id)) {
      nextMeta._diffStatus = 'added'
      delete nextMeta._diffGhost
    } else if (modifiedSet.has(id)) {
      nextMeta._diffStatus = 'modified'
      delete nextMeta._diffGhost
    } else {
      delete nextMeta._diffStatus
      delete nextMeta._diffGhost
    }
    mergedNodes.push({ ...n, meta: nextMeta })
  }

  removedSet.forEach((id) => {
    const prev = prevNodes.get(String(id))
    if (!prev) return
    const nextMeta = { ...(prev.meta || {}) }
    nextMeta._diffStatus = 'deleted'
    nextMeta._diffGhost = true
    mergedNodes.push({ ...prev, meta: nextMeta })
  })

  const mergedEdges = mergeEdgesForPendingEdit(prevGraph, nextGraph)
  return {
    pendingGraph: { nodes: mergedNodes, edges: mergedEdges },
    finalGraph: nextGraph,
  }
}

function clearDiffFromGraph(graph) {
  return {
    nodes: (graph?.nodes || []).map((n) => {
      if (!n?.meta || !('_diffStatus' in n.meta)) return n
      const nextMeta = { ...(n.meta || {}) }
      delete nextMeta._diffStatus
      delete nextMeta._diffGhost
      return { ...n, meta: nextMeta }
    }),
    edges: [...(graph?.edges || [])],
  }
}

/** 从 localStorage 恢复 assistantPending；结构不合法时返回 null */
function normalizeRestoredAssistantPending(raw) {
  if (!raw || typeof raw !== 'object') return null
  if (raw.kind === 'edit') {
    const prev = raw.prev
    if (!prev || typeof prev !== 'object') return null
    if (!Array.isArray(prev.graphData?.nodes)) return null
    if (typeof prev.rawJsonText !== 'string') return null
    if (!raw.finalGraph || !Array.isArray(raw.finalGraph.nodes)) return null
    const diff = raw.diff && typeof raw.diff === 'object'
      ? {
          added: Array.isArray(raw.diff.added) ? raw.diff.added.map(String) : [],
          modified: Array.isArray(raw.diff.modified) ? raw.diff.modified.map(String) : [],
          removed: Array.isArray(raw.diff.removed) ? raw.diff.removed.map(String) : [],
        }
      : { added: [], modified: [], removed: [] }
    return {
      kind: 'edit',
      prev: {
        graphData: prev.graphData,
        rawJsonText: prev.rawJsonText,
      },
      finalGraph: raw.finalGraph,
      diff,
      at: Number.isFinite(Number(raw.at)) ? Number(raw.at) : Date.now(),
    }
  }
  if (raw.kind === 'generate') {
    const prev = raw.prev
    if (!prev || typeof prev !== 'object') return null
    if (!Array.isArray(prev.graphData?.nodes)) return null
    if (typeof prev.rawJsonText !== 'string') return null
    return {
      kind: 'generate',
      backendTreeId: raw.backendTreeId ? String(raw.backendTreeId) : '',
      deleteOnUndo: !!raw.deleteOnUndo,
      prev: {
        graphData: prev.graphData,
        rawJsonText: prev.rawJsonText,
      },
    }
  }
  return null
}

/** 用于判断「是否与加载时一致」：忽略空白与 key 顺序差异 */
function canonicalJsonString(text) {
  if (typeof text !== 'string') return ''
  try {
    return JSON.stringify(JSON.parse(text))
  } catch {
    return text.trim()
  }
}

function normalizeToGraph(raw) {
  if (!raw) return { nodes: [], edges: [] }
  if (
    raw.tree_data &&
    Array.isArray(raw.tree_data.nodeList) &&
    Array.isArray(raw.tree_data.linkList)
  ) {
    return parseRawFtaJson(raw.tree_data)
  }
  if (Array.isArray(raw.nodeList) && Array.isArray(raw.linkList)) {
    return parseRawFtaJson(raw)
  }
  if (raw.tree_data && raw.tree_data.nodes) {
    return parseTreeDataJson(raw)
  }
  if (Array.isArray(raw.nodes) && Array.isArray(raw.edges)) {
    return {
      nodes: raw.nodes.map((n) => ({
        id: String(n.id),
        label: n.label || n.name || n.title || String(n.id),
        type: n.type || 'event',
        level: n.level ?? n.depth ?? 0,
        meta: n.meta ?? n,
      })),
      edges: raw.edges.map((e, idx) => ({
        id: e.id ? String(e.id) : `e-${idx}`,
        source: String(e.source),
        target: String(e.target),
        relation: e.relation || e.gate || '',
        meta: e.meta ?? e,
      })),
    }
  }
  return { nodes: [], edges: [] }
}

function extractTreeDataForBackend({ rawJsonText, graphData, parsedInfo }) {
  // 优先使用 rawJsonText 中已有的 nodeList/linkList，避免丢字段
  try {
    const parsed = JSON.parse(rawJsonText)
    if (parsed?.tree_data?.nodeList && parsed?.tree_data?.linkList) return parsed.tree_data
    if (parsed?.nodeList && parsed?.linkList) return parsed
  } catch {
    // ignore
  }

  // fallback：用当前画布 graphData 生成 nodeList/linkList（后端使用 string 类型）
  const raw = graphToRawJson(graphData, parsedInfo?.attr, { stringEventTypes: true })
  return { nodeList: raw.nodeList, linkList: raw.linkList }
}

function splitEmbeddedValidation(obj) {
  if (!obj || typeof obj !== 'object') {
    return { trimmed: obj, logic: null, ai: null }
  }

  // clone shallowly; deep clone only where needed
  const clone = Array.isArray(obj) ? [...obj] : { ...obj }

  // validation may exist at:
  // - root.validation
  // - root.tree_data.validation
  // - root.tree_data.tree_data.validation (rare)
  const rootValidation = clone.validation
  if (rootValidation && typeof rootValidation === 'object') {
    delete clone.validation
  }

  const td = clone.tree_data && typeof clone.tree_data === 'object' ? { ...clone.tree_data } : null
  let treeValidation = null
  if (td?.validation && typeof td.validation === 'object') {
    treeValidation = td.validation
    delete td.validation
    clone.tree_data = td
  }

  const v = treeValidation || rootValidation
  if (!v || typeof v !== 'object') {
    return { trimmed: clone, logic: null, ai: null }
  }

  const issues = Array.isArray(v.issues) ? v.issues : []
  const logicIssues = issues.filter((i) => (i?.source || 'logic') === 'logic')
  const aiIssues = issues.filter((i) => (i?.source || 'logic') === 'ai')

  const buildBucket = (bucket) => {
    const error_count = bucket.filter((i) => i?.level === 'ERROR').length
    const warning_count = bucket.filter((i) => i?.level === 'WARNING').length
    const info_count = bucket.filter((i) => i?.level === 'INFO').length
    return {
      passed: error_count === 0,
      error_count,
      warning_count,
      info_count,
      issues: bucket,
    }
  }

  return {
    trimmed: clone,
    logic: logicIssues.length ? buildBucket(logicIssues) : null,
    ai: aiIssues.length ? buildBucket(aiIssues) : null,
  }
}

function extractObjectRefFromDescription(description) {
  if (typeof description !== 'string') return ''
  const m = description.match(/\[Ref:\s*(Object_\d+)\]\s*$/)
  return m ? m[1] : ''
}

function FaultTreePage() {
  const { theme } = useTheme()
  const navigate = useNavigate()
  const location = useLocation()
  const projectIdFromQuery = useMemo(() => {
    const params = new URLSearchParams(location.search)
    return params.get('projectId') || ''
  }, [location.search])
  const treeIdFromQuery = useMemo(() => {
    const params = new URLSearchParams(location.search)
    return params.get('faultTreeId') || params.get('treeId') || ''
  }, [location.search])
  const canvasIdFromQuery = useMemo(() => {
    const params = new URLSearchParams(location.search)
    return params.get('canvasId') || ''
  }, [location.search])
  const initialState = useMemo(() => {
    if (typeof window === 'undefined') {
      return {
        raw: JSON.stringify(rawFtaSample, null, 2),
        graph: normalizeToGraph(rawFtaSample),
      }
    }
    try {
      const params = new URLSearchParams(window.location.search)
      const blank = params.get('blank')
      const snap = params.get('snapshot')
      const canvasId = params.get('canvasId') || ''
      const projectId = params.get('projectId') || ''
      const tid = params.get('faultTreeId') || params.get('treeId') || ''
      if (canvasId && projectId && !tid) {
        const map = readCanvasDraftMap(projectId)
        const d = map[canvasId]
        if (d?.rawJsonText) {
          try {
            const parsed = JSON.parse(d.rawJsonText)
            const g = d.graphData || normalizeToGraph(parsed)
            return { raw: d.rawJsonText, graph: g }
          } catch {
            /* fall through to empty */
          }
        }
        const empty = { nodes: [], edges: [] }
        return { raw: JSON.stringify(empty, null, 2), graph: normalizeToGraph(empty) }
      }
      if (blank === '1' || blank === 'true') {
        const empty = { nodes: [], edges: [] }
        return { raw: JSON.stringify(empty, null, 2), graph: normalizeToGraph(empty) }
      }
      if (!snap) {
        return {
          raw: JSON.stringify(rawFtaSample, null, 2),
          graph: normalizeToGraph(rawFtaSample),
        }
      }
      const parsed = JSON.parse(snap)
      return {
        raw: JSON.stringify(parsed, null, 2),
        graph: normalizeToGraph(parsed),
      }
    } catch {
      return {
        raw: JSON.stringify(rawFtaSample, null, 2),
        graph: normalizeToGraph(rawFtaSample),
      }
    }
  }, [])

  const [rawJsonText, setRawJsonText] = useState(initialState.raw)
  /** 页面加载/从后端拉取/保存成功后的基准 JSON，用于禁用「无修改时提交」 */
  const [baselineJsonText, setBaselineJsonText] = useState(() =>
    canonicalJsonString(initialState.raw),
  )
  const [graphData, setGraphData] = useState(initialState.graph)
  const [viewMode, setViewMode] = useState('type')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [backendTreeId, setBackendTreeId] = useState(treeIdFromQuery || '')
  const [backendVersion, setBackendVersion] = useState(null)
  const [backendLoading, setBackendLoading] = useState(false)
  const [selectedNode, setSelectedNode] = useState(null)
  const [selectedEdge, setSelectedEdge] = useState(null)
  const [explodedHighlightNodeName, setExplodedHighlightNodeName] = useState('')
  /** 每次在画布选中节点且爆炸图已打开时递增，保证 ExplodedViewer 立即/切换时同步高亮 */
  const [explodedHighlightSync, setExplodedHighlightSync] = useState(0)
  /** 项目页「三维」上传的 GLB / 部件表（IndexedDB） */
  const [exploded3dGlbObjectUrl, setExploded3dGlbObjectUrl] = useState('')
  const [exploded3dPartDetails, setExploded3dPartDetails] = useState(null)
  const [chunkPanelOpen, setChunkPanelOpen] = useState(false)
  const [chunkPanelChunkIds, setChunkPanelChunkIds] = useState([])
  const [chunkPanelActiveId, setChunkPanelActiveId] = useState(null)
  const [chunkPanelData, setChunkPanelData] = useState({})
  const [chunkPanelLoading, setChunkPanelLoading] = useState(false)
  const [chunkPanelError, setChunkPanelError] = useState('')
  const [exporting, setExporting] = useState(false)
  const [contextMenu, setContextMenu] = useState(null)
  const [edgeMenu, setEdgeMenu] = useState(null)
  const [paneMenu, setPaneMenu] = useState(null)
  const [modal, setModal] = useState(null)
  const [validation, setValidation] = useState(null)
  const [validationLoading, setValidationLoading] = useState(false)
  const [validationError, setValidationError] = useState('')
  const [showValidationPanel, setShowValidationPanel] = useState(true)
  const [aiEmbeddedValidation, setAiEmbeddedValidation] = useState(null)
  const [aiSemanticValidation, setAiSemanticValidation] = useState(null)
  const [aiSemanticLoading, setAiSemanticLoading] = useState(false)
  const [aiSemanticError, setAiSemanticError] = useState('')
  const [aiSemanticNotice, setAiSemanticNotice] = useState('')
  const [aiSemanticLastSig, setAiSemanticLastSig] = useState('')
  const [aiValidation, setAiValidation] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState('')
  const [showAiPanel, setShowAiPanel] = useState(true)
  const [history, setHistory] = useState([])
  const [redoHistory, setRedoHistory] = useState([])
  const [exportModalOpen, setExportModalOpen] = useState(false)
  /** 后端返回的版本元数据列表（不含 tree_data） */
  const [versionList, setVersionList] = useState([])
  const [_historyLoading, setHistoryLoading] = useState(false)
  const [_historyError, setHistoryError] = useState('')
  const [jsonPanelOpen, setJsonPanelOpen] = useState(true)
  const [explodedPanelOpen, setExplodedPanelOpen] = useState(false)
  const [_backendMeta, setBackendMeta] = useState({
    editor: '',
    created_at: null,
    description: '',
    is_ai_generated: false,
  })
  /** 用户是否已通过右上角「提交」将当前版本保存到 FTA-Latest 后端 */
  const [submittedToBackend, setSubmittedToBackend] = useState(false)
  const canvasDraftHydratedRef = useRef(false)
  /** 供离开页面前 flush 画布草稿（含 assistantPending） */
  const canvasDraftStateRef = useRef(null)
  const assistantPersistRef = useRef(null)
  /** 已对齐的项目知识库 kbDatasetEpoch；用于判断项目页是否变更了来源文件 */
  const kbSyncedBaselineEpochRef = useRef(0)
  /**
   * 已对齐的“依据文件 file_version_id”签名。
   * 只有当该签名真正变化时，才允许在非空故障树上触发 generate（否则一律走 fta-ai-service 修改接口）。
   */
  const kbSyncedBaselineFvSigRef = useRef('')
  /** 下一条用户消息是否须走 FTA /api/tree/generate（与 epoch 漂移二选一满足即可） */
  const requireGenerateViaGenerateRef = useRef(false)
  const assistantStoreKey = useMemo(() => {
    const pid = projectIdFromQuery || 'no-project'
    if (treeIdFromQuery) return `${pid}:tree-${treeIdFromQuery}`
    if (canvasIdFromQuery) return `${pid}:canvas-${canvasIdFromQuery}`
    return `${pid}:${backendTreeId || 'local'}`
  }, [projectIdFromQuery, treeIdFromQuery, canvasIdFromQuery, backendTreeId])

  const [assistantMessages, setAssistantMessages] = useState(() => [
    {
      id: `a-${Date.now()}`,
      role: 'assistant',
      kind: 'text',
      content: '我是故障树画布页的 AI 助手。你可以直接说“新增一个底事件/重命名某节点/删除某分支”等，我会先在画布上高亮展示改动，然后你可以 Accept 或 Undo。',
      at: Date.now(),
    },
  ])
  const [assistantInput, setAssistantInput] = useState('')
  const [assistantBusy, setAssistantBusy] = useState(false)
  const [assistantPending, setAssistantPending] = useState(null)
  const [assistantSnapshots, setAssistantSnapshots] = useState([])
  const [assistantTasks, setAssistantTasks] = useState([])
  const [assistantProgressModal, setAssistantProgressModal] = useState({ open: false, taskId: '' })
  const assistantTaskEventHistoryRef = useRef(new Map())
  /** 仅 setState 触发重绘，使进度详情模态读到合并后的 job-item.events */
  const [, setAssistantTaskEventsRevision] = useState(0)
  const [selectedSourceFiles, setSelectedSourceFiles] = useState(() => readSelectedFiles(assistantStoreKey))
  const [assistantOpen, setAssistantOpen] = useState(true)
  const [projectFiles, setProjectFiles] = useState([])
  /** null 表示未配置（画布显示全部项目文件）；数组为项目页勾选的知识库来源 id */
  const [kbAllowList, setKbAllowList] = useState(null)
  const canvasRef = useRef(null)
  const canvasActionsRef = useRef(null)
  const sidePanelRef = useRef(null)
  const assistantPanelRef = useRef(null)

  const [sidePanelWidth, setSidePanelWidth] = useState(() => {
    const raw = localStorage.getItem('fta:sidePanelWidth')
    const n = Number(raw)
    return Number.isFinite(n) && n > 0 ? n : 340
  })

  useEffect(() => {
    try {
      localStorage.setItem('fta:sidePanelWidth', String(sidePanelWidth))
    } catch {
      // ignore
    }
  }, [sidePanelWidth])

  const sidePanelOpen = jsonPanelOpen || explodedPanelOpen

  const [assistantPanelWidth, setAssistantPanelWidth] = useState(() => {
    const raw = localStorage.getItem('fta:assistantPanelWidth')
    const n = Number(raw)
    return Number.isFinite(n) && n > 0 ? n : 420
  })

  useEffect(() => {
    try {
      localStorage.setItem('fta:assistantPanelWidth', String(assistantPanelWidth))
    } catch {
      // ignore
    }
  }, [assistantPanelWidth])

  const handleSidePanelResizeStart = useCallback(
    (e) => {
      if (!sidePanelOpen) return
      const el = sidePanelRef.current
      if (!el) return
      e.preventDefault()
      e.stopPropagation()

      const rect = el.getBoundingClientRect()
      const startX = e.clientX
      const startW = rect.width

      const minW = explodedPanelOpen ? 360 : 240
      const maxW = Math.max(minW, Math.min(window.innerWidth - 360, 980))

      const onMove = (ev) => {
        const delta = ev.clientX - startX
        const next = Math.round(startW + delta)
        setSidePanelWidth(Math.max(minW, Math.min(maxW, next)))
      }

      const onUp = () => {
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
      }

      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    },
    [sidePanelOpen, explodedPanelOpen],
  )

  const handleAssistantResizeStart = useCallback(
    (e) => {
      if (!assistantOpen) return
      const el = assistantPanelRef.current
      if (!el) return
      e.preventDefault()
      e.stopPropagation()

      const rect = el.getBoundingClientRect()
      const startX = e.clientX
      const startW = rect.width

      const minW = 320
      const maxW = Math.max(minW, Math.min(window.innerWidth - 520, 900))

      const onMove = (ev) => {
        // 拖拽分隔条：向右拖 => assistant 变窄；向左拖 => assistant 变宽
        const delta = ev.clientX - startX
        const next = Math.round(startW - delta)
        setAssistantPanelWidth(Math.max(minW, Math.min(maxW, next)))
      }

      const onUp = () => {
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
      }

      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    },
    [assistantOpen],
  )

  const handleNodeSelect = useCallback(
    (node) => {
      setSelectedNode(node)
      setSelectedEdge(null)
      if (!explodedPanelOpen) return
      const desc = node?.data?.meta?.event?.description ?? ''
      const ref = extractObjectRefFromDescription(desc)
      setExplodedHighlightNodeName(ref || '')
      setExplodedHighlightSync((s) => s + 1)
    },
    [explodedPanelOpen],
  )

  const handleEdgeSelect = useCallback(
    (edge) => {
      setSelectedEdge(edge)
      setSelectedNode(null)
    },
    [],
  )

  /** 侧栏从关到开时，用当前选中节点同步一次三维高亮 */
  useEffect(() => {
    if (!explodedPanelOpen) return
    if (!selectedNode) return
    const desc = selectedNode?.data?.meta?.event?.description ?? ''
    const ref = extractObjectRefFromDescription(desc)
    setExplodedHighlightNodeName(ref || '')
    setExplodedHighlightSync((s) => s + 1)
  }, [explodedPanelOpen])

  // 高保真导出需要把故障树 JSON 放在 querystring 里（/fta-viewer?snapshot=...），
  // 为避免 JSON 太长触发 431，这里只保留“渲染所需”的最小字段，删除 description/message 等无关内容。
  const hiResSnapshotText = useMemo(() => {
    const snapshot = {
      nodes: (graphData.nodes || []).map((n) => {
        const out = {
          id: String(n.id),
          label: n.label,
          type: n.type || 'event',
        }
        const err =
          n?.meta?.event?.errorLevel ??
          n?.meta?.errorLevel ??
          n?.meta?.level ??
          undefined
        if (err !== undefined) out.meta = { event: { errorLevel: err } }
        return out
      }),
      edges: (graphData.edges || []).map((e, idx) => ({
        id: e.id ? String(e.id) : `e-${idx}`,
        source: String(e.source),
        target: String(e.target),
      })),
    }
    try {
      return JSON.stringify(snapshot)
    } catch {
      return ''
    }
  }, [graphData.nodes, graphData.edges])

  // 用于判断“是否有新的修改”：基于画布的最小签名（与高保真快照同源，稳定且成本低）
  const editSignature = useMemo(() => {
    // hiResSnapshotText 可能为空（序列化失败），这里做兜底
    if (typeof hiResSnapshotText === 'string' && hiResSnapshotText) return hiResSnapshotText
    try {
      return JSON.stringify({
        n: (graphData.nodes || []).map((n) => ({
          id: String(n.id),
          label: n.label,
          type: n.type || 'event',
        })),
        e: (graphData.edges || []).map((e) => ({
          s: String(e.source),
          t: String(e.target),
        })),
      })
    } catch {
      return ''
    }
  }, [hiResSnapshotText, graphData.nodes, graphData.edges])

  const aiSemanticStale = useMemo(() => {
    if (!aiSemanticLastSig) return false
    if (!editSignature) return false
    return aiSemanticLastSig !== editSignature
  }, [aiSemanticLastSig, editSignature])

  const hasUnsavedChanges = useMemo(
    () => canonicalJsonString(rawJsonText) !== canonicalJsonString(baselineJsonText),
    [rawJsonText, baselineJsonText],
  )

  const sortedVersionOptions = useMemo(() => {
    if (!versionList.length) return []
    return [...versionList].sort((a, b) => b.version - a.version)
  }, [versionList])

  /** 历史列表尚未返回时，用当前 backendVersion + 已加载元数据占位，保证下拉有当前选项 */
  const versionSelectOptions = useMemo(() => {
    if (sortedVersionOptions.length > 0) return sortedVersionOptions
    if (backendVersion != null && Number.isFinite(Number(backendVersion))) {
      return [
        {
          version: Number(backendVersion),
          editor: _backendMeta?.editor || '—',
          created_at: _backendMeta?.created_at ?? null,
          description: _backendMeta?.description || '当前已加载版本',
          is_ai_generated: !!_backendMeta?.is_ai_generated,
        },
      ]
    }
    return []
  }, [sortedVersionOptions, backendVersion, _backendMeta])

  const latestVersion = useMemo(() => {
    if (!versionList.length) return null
    return Math.max(...versionList.map((v) => v.version))
  }, [versionList])

  const _viewingHistorical = useMemo(
    () =>
      Boolean(
        backendVersion != null &&
          latestVersion != null &&
          backendVersion < latestVersion,
      ),
    [backendVersion, latestVersion],
  )

  const applyBackendVersionPayload = useCallback((ver) => {
    const { trimmed, logic, ai } = splitEmbeddedValidation(ver)
    const loadedText = JSON.stringify(trimmed, null, 2)
    setRawJsonText(loadedText)
    setBaselineJsonText(canonicalJsonString(loadedText))
    setGraphData(normalizeToGraph(trimmed))
    setBackendVersion(ver?.version ?? null)
    setBackendMeta({
      editor: ver?.editor || '',
      created_at: ver?.created_at ?? null,
      description: ver?.description || '',
      is_ai_generated: !!ver?.is_ai_generated,
    })
    if (logic) setValidation(logic)
    else setValidation(null)
    if (ai) setAiEmbeddedValidation(ai)
    else setAiEmbeddedValidation(null)
    setHistory([])
    setRedoHistory([])
    setError('')
    setAiSemanticLastSig('')
    setAiSemanticValidation(null)
    setAiSemanticNotice('')
    setSelectedNode(null)
    setSelectedEdge(null)
    setChunkPanelOpen(false)
    setChunkPanelChunkIds([])
    setChunkPanelActiveId(null)
    setChunkPanelData({})
  }, [])

  const handleVersionSelect = useCallback(
    async (nextVersion) => {
      if (!backendTreeId || !Number.isFinite(nextVersion)) return
      if (nextVersion === backendVersion) return
      setBackendLoading(true)
      setError('')
      try {
        const ver = await getTreeVersion({ treeId: backendTreeId, version: nextVersion })
        applyBackendVersionPayload(ver)
      } catch (e) {
        if (e?.name === 'AbortError') return
        const msg = String(e?.message || '')
        if (msg.toLowerCase().includes('aborted') || msg.toLowerCase().includes('signal is aborted')) return
        console.error(e)
        setError(e?.message || '加载该版本失败')
      } finally {
        setBackendLoading(false)
      }
    },
    [backendTreeId, backendVersion, applyBackendVersionPayload],
  )

  useEffect(() => {
    // 一旦用户又改了画布，把“没有新的修改”提示清掉，避免误导
    if (aiSemanticNotice) setAiSemanticNotice('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editSignature])

  useEffect(() => {
    if (!contextMenu) return
    const dismiss = () => setContextMenu(null)
    const timer = setTimeout(() => document.addEventListener('click', dismiss), 0)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('click', dismiss)
    }
  }, [contextMenu])

  useEffect(() => {
    if (!paneMenu) return
    const dismiss = () => setPaneMenu(null)
    const timer = setTimeout(() => document.addEventListener('click', dismiss), 0)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('click', dismiss)
    }
  }, [paneMenu])

  const parsedInfo = useMemo(() => {
    try {
      const parsed = JSON.parse(rawJsonText)
      if (
        parsed.tree_data &&
        Array.isArray(parsed.tree_data.nodeList) &&
        Array.isArray(parsed.tree_data.linkList)
      ) {
        return {
          parsed,
          kind: 'rawFta',
          attr: parsed.attr,
          wrapper: parsed,
        }
      }
      if (Array.isArray(parsed.nodeList) && Array.isArray(parsed.linkList)) {
        return {
          parsed,
          kind: 'rawFta',
          attr: parsed.attr,
          wrapper: null,
        }
      }
      if (parsed.tree_data && parsed.tree_data.nodes) {
        return { parsed, kind: 'treeData', attr: undefined, wrapper: null }
      }
      if (Array.isArray(parsed.nodes) && Array.isArray(parsed.edges)) {
        return { parsed, kind: 'graph', attr: undefined, wrapper: null }
      }
      return { parsed, kind: 'unknown', attr: undefined, wrapper: null }
    } catch {
      return { parsed: null, kind: 'invalid', attr: undefined, wrapper: null }
    }
  }, [rawJsonText])

  const applyEdit = useCallback(
    (newGraphData) => {
      setHistory((h) => [...h, { graphData, rawJsonText }])
      setRedoHistory([])
      setGraphData(newGraphData)
      let nextJson
      if (parsedInfo.kind === 'rawFta' && parsedInfo.parsed) {
        const core = graphToRawJson(newGraphData, parsedInfo.attr, {
          stringEventTypes: !!parsedInfo.wrapper,
        })
        if (parsedInfo.wrapper) {
          const w = parsedInfo.wrapper
          nextJson = {
            ...w,
            tree_data: {
              ...w.tree_data,
              nodeList: core.nodeList,
              linkList: core.linkList,
            },
          }
        } else {
          nextJson = core
        }
      } else if (parsedInfo.kind === 'treeData' && parsedInfo.parsed) {
        nextJson = graphToTreeDataJson(newGraphData, parsedInfo.parsed)
      } else if (parsedInfo.kind === 'graph' && parsedInfo.parsed) {
        nextJson = {
          ...parsedInfo.parsed,
          nodes: newGraphData.nodes,
          edges: newGraphData.edges,
        }
      } else {
        nextJson = graphToRawJson(newGraphData, parsedInfo.attr)
      }
      setRawJsonText(JSON.stringify(nextJson, null, 2))
      setError('')
    },
    [parsedInfo, graphData, rawJsonText],
  )

  const syncRawFromGraph = useCallback(
    (newGraphData) => {
      let nextJson
      if (parsedInfo.kind === 'rawFta' && parsedInfo.parsed) {
        const core = graphToRawJson(newGraphData, parsedInfo.attr, {
          stringEventTypes: !!parsedInfo.wrapper,
        })
        if (parsedInfo.wrapper) {
          const w = parsedInfo.wrapper
          nextJson = {
            ...w,
            tree_data: {
              ...w.tree_data,
              nodeList: core.nodeList,
              linkList: core.linkList,
            },
          }
        } else {
          nextJson = core
        }
      } else if (parsedInfo.kind === 'treeData' && parsedInfo.parsed) {
        nextJson = graphToTreeDataJson(newGraphData, parsedInfo.parsed)
      } else if (parsedInfo.kind === 'graph' && parsedInfo.parsed) {
        nextJson = {
          ...parsedInfo.parsed,
          nodes: newGraphData.nodes,
          edges: newGraphData.edges,
        }
      } else {
        nextJson = graphToRawJson(newGraphData, parsedInfo.attr)
      }
      return JSON.stringify(nextJson, null, 2)
    },
    [parsedInfo],
  )

  const restoreSnapshot = useCallback(
    (snap) => {
      if (!snap) return
      const nextRaw = typeof snap.rawJsonText === 'string' ? snap.rawJsonText : rawJsonText
      const nextGraph = snap.graphData ? snap.graphData : graphData
      setRawJsonText(nextRaw)
      setBaselineJsonText(canonicalJsonString(nextRaw))
      setGraphData(nextGraph)
      setHistory([])
      setRedoHistory([])
      setSelectedNode(null)
      setSelectedEdge(null)
      setError('')
      setNotice('')
    },
    [graphData, rawJsonText],
  )

  const persistAssistantState = useCallback(
    (next) => {
      writeAiAssistStore(assistantStoreKey, next)
    },
    [assistantStoreKey],
  )

  const flushCanvasDraftNow = useCallback(() => {
    const s = canvasDraftStateRef.current
    if (!s?.canvasIdFromQuery || !s?.projectIdFromQuery) return
    upsertCanvasDraft(s.projectIdFromQuery, s.canvasIdFromQuery, {
      title: inferCanvasDisplayTitle(s.rawJsonText, s.graphData),
      rawJsonText: s.rawJsonText,
      baselineJsonText: s.baselineJsonText,
      graphData: s.graphData,
      backendTreeId: s.backendTreeId,
      submittedToBackend: s.submittedToBackend,
      assistantMessages: s.assistantMessages,
      assistantSnapshots: s.assistantSnapshots,
      assistantTasks: s.assistantTasks,
      taskEventHistory: Object.fromEntries(assistantTaskEventHistoryRef.current.entries()),
      selectedSourceFiles: s.selectedSourceFiles,
      assistantPending: s.assistantPending ?? null,
      assistantKbBaselineEpoch: s.assistantKbBaselineEpoch ?? 0,
      assistantKbBaselineFvSig: s.assistantKbBaselineFvSig ?? '',
    })
  }, [])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const params = new URLSearchParams(window.location.search)
    const b = params.get('blank')
    if (b !== '1' && b !== 'true') return
    if (!projectIdFromQuery) return
    const cid = `canvas-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
    navigate(
      `/fta-viewer?projectId=${encodeURIComponent(projectIdFromQuery)}&canvasId=${encodeURIComponent(cid)}`,
      { replace: true },
    )
  }, [projectIdFromQuery, navigate, location.search])

  /** 画布会话：从草稿恢复助手状态（图与 JSON 已在 initialState 中同步加载） */
  useEffect(() => {
    if (!canvasIdFromQuery || !projectIdFromQuery || canvasDraftHydratedRef.current) return
    canvasDraftHydratedRef.current = true
    const d = readCanvasDraftMap(projectIdFromQuery)[canvasIdFromQuery]
    if (!d) return
    if (typeof d.submittedToBackend === 'boolean') setSubmittedToBackend(d.submittedToBackend)
    if (Array.isArray(d.assistantMessages) && d.assistantMessages.length) {
      setAssistantMessages(d.assistantMessages.slice(-320))
    }
    if (Array.isArray(d.assistantSnapshots)) setAssistantSnapshots(d.assistantSnapshots.slice(-120))
    if (Array.isArray(d.assistantTasks)) setAssistantTasks(d.assistantTasks.slice(-80))
    try {
      const entries = d.taskEventHistory
      if (entries && typeof entries === 'object') {
        const hist = new Map()
        Object.entries(entries).forEach(([k, v]) => {
          if (Array.isArray(v)) hist.set(k, v)
        })
        assistantTaskEventHistoryRef.current = hist
      }
    } catch {
      assistantTaskEventHistoryRef.current = new Map()
    }
    if (Array.isArray(d.selectedSourceFiles)) setSelectedSourceFiles(d.selectedSourceFiles.map(String).filter(Boolean))
    if (d.backendTreeId) setBackendTreeId(String(d.backendTreeId))
    if (typeof d.baselineJsonText === 'string' && d.baselineJsonText.trim()) {
      setBaselineJsonText(canonicalJsonString(d.baselineJsonText))
    }
    const restoredPending = normalizeRestoredAssistantPending(d.assistantPending)
    if (restoredPending) setAssistantPending(restoredPending)
    if (typeof d.assistantKbBaselineEpoch === 'number' && Number.isFinite(d.assistantKbBaselineEpoch)) {
      kbSyncedBaselineEpochRef.current = d.assistantKbBaselineEpoch
    } else {
      const ws = getWorkspace(projectIdFromQuery)
      kbSyncedBaselineEpochRef.current = Number(ws?.kbDatasetEpoch) || 0
    }
    if (typeof d.assistantKbBaselineFvSig === 'string') {
      kbSyncedBaselineFvSigRef.current = d.assistantKbBaselineFvSig
    }
  }, [canvasIdFromQuery, projectIdFromQuery])

  // restore assistant state（非画布会话 id 时沿用旧 key）
  useEffect(() => {
    if (canvasIdFromQuery) return
    const payload = readAiAssistStore(assistantStoreKey)
    if (payload && typeof payload === 'object') {
      if (Array.isArray(payload.messages) && payload.messages.length) {
        setAssistantMessages(payload.messages.slice(-320))
      }
      if (Array.isArray(payload.snapshots)) {
        setAssistantSnapshots(payload.snapshots.slice(-120))
      }
      if (Array.isArray(payload.tasks)) {
        setAssistantTasks(payload.tasks.slice(-80))
      }
      try {
        const hist = new Map()
        const entries = payload.taskEventHistory
        if (entries && typeof entries === 'object') {
          Object.entries(entries).forEach(([k, v]) => {
            if (Array.isArray(v)) hist.set(k, v)
          })
        }
        assistantTaskEventHistoryRef.current = hist
      } catch {
        assistantTaskEventHistoryRef.current = new Map()
      }
      const restoredPending = normalizeRestoredAssistantPending(payload?.assistantPending)
      if (restoredPending) setAssistantPending(restoredPending)
    }
    if (projectIdFromQuery) {
      const fromStore =
        payload && typeof payload?.assistantKbBaselineEpoch === 'number' && Number.isFinite(payload.assistantKbBaselineEpoch)
          ? payload.assistantKbBaselineEpoch
          : null
      kbSyncedBaselineEpochRef.current =
        fromStore != null ? fromStore : Number(getWorkspace(projectIdFromQuery)?.kbDatasetEpoch) || 0
      if (payload && typeof payload?.assistantKbBaselineFvSig === 'string') {
        kbSyncedBaselineFvSigRef.current = payload.assistantKbBaselineFvSig
      }
    }
  }, [assistantStoreKey, canvasIdFromQuery, projectIdFromQuery])

  // persist assistant state (best-effort) — 画布会话由下方草稿统一持久化
  useEffect(() => {
    if (canvasIdFromQuery) return
    persistAssistantState({
      messages: assistantMessages,
      snapshots: assistantSnapshots,
      tasks: assistantTasks,
      taskEventHistory: Object.fromEntries(assistantTaskEventHistoryRef.current.entries()),
      assistantPending: assistantPending ?? null,
      assistantKbBaselineEpoch: kbSyncedBaselineEpochRef.current,
      assistantKbBaselineFvSig: kbSyncedBaselineFvSigRef.current,
    })
  }, [
    assistantMessages,
    assistantSnapshots,
    assistantTasks,
    assistantPending,
    persistAssistantState,
    canvasIdFromQuery,
  ])

  /** 有待处理 AI 修改时立即写入，避免仅依赖防抖导致离开页面前未保存 */
  useLayoutEffect(() => {
    if (canvasIdFromQuery) return
    if (!assistantPending) return
    persistAssistantState({
      messages: assistantMessages,
      snapshots: assistantSnapshots,
      tasks: assistantTasks,
      taskEventHistory: Object.fromEntries(assistantTaskEventHistoryRef.current.entries()),
      assistantPending,
      assistantKbBaselineEpoch: kbSyncedBaselineEpochRef.current,
      assistantKbBaselineFvSig: kbSyncedBaselineFvSigRef.current,
    })
  }, [
    assistantPending,
    canvasIdFromQuery,
    assistantMessages,
    assistantSnapshots,
    assistantTasks,
    persistAssistantState,
  ])

  /** 画布会话：将 JSON、助手对话、待 Accept/Undo 状态写入 localStorage */
  useEffect(() => {
    if (!canvasIdFromQuery || !projectIdFromQuery) return
    const delay = assistantPending ? 0 : 450
    const t = window.setTimeout(() => {
      flushCanvasDraftNow()
    }, delay)
    return () => {
      clearTimeout(t)
      flushCanvasDraftNow()
    }
  }, [
    canvasIdFromQuery,
    projectIdFromQuery,
    rawJsonText,
    baselineJsonText,
    graphData,
    backendTreeId,
    submittedToBackend,
    assistantMessages,
    assistantSnapshots,
    assistantTasks,
    selectedSourceFiles,
    assistantPending,
    flushCanvasDraftNow,
  ])

  useEffect(() => {
    if (!canvasIdFromQuery || !projectIdFromQuery) return undefined
    const onPageHide = () => flushCanvasDraftNow()
    window.addEventListener('pagehide', onPageHide)
    return () => window.removeEventListener('pagehide', onPageHide)
  }, [canvasIdFromQuery, projectIdFromQuery, flushCanvasDraftNow])

  useEffect(() => {
    if (canvasIdFromQuery) return undefined
    const onPageHide = () => {
      const p = assistantPersistRef.current
      if (!p) return
      persistAssistantState({
        messages: p.messages,
        snapshots: p.snapshots,
        tasks: p.tasks,
        taskEventHistory: Object.fromEntries(assistantTaskEventHistoryRef.current.entries()),
        assistantPending: p.assistantPending ?? null,
        assistantKbBaselineEpoch: kbSyncedBaselineEpochRef.current,
        assistantKbBaselineFvSig: kbSyncedBaselineFvSigRef.current,
      })
    }
    window.addEventListener('pagehide', onPageHide)
    return () => window.removeEventListener('pagehide', onPageHide)
  }, [canvasIdFromQuery, persistAssistantState])

  useEffect(() => {
    writeSelectedFiles(assistantStoreKey, selectedSourceFiles)
  }, [assistantStoreKey, selectedSourceFiles])

  const refreshProjectKbContext = useCallback(() => {
    if (!projectIdFromQuery) {
      setProjectFiles([])
      setKbAllowList(null)
      return
    }
    const ws = getWorkspace(projectIdFromQuery)
    const hydratedFiles = ensureMockImportedKbFile(Array.isArray(ws?.files) ? ws.files : [])
    setProjectFiles(hydratedFiles)
    const raw = ws?.kbSourceFileIds
    setKbAllowList(
      Array.isArray(raw)
        ? Array.from(new Set([MOCK_IMPORTED_KB_FILE.id, ...raw.map(String)]))
        : null,
    )
    const ep = typeof ws?.kbDatasetEpoch === 'number' ? ws.kbDatasetEpoch : 0
    if (ep > kbSyncedBaselineEpochRef.current) {
      requireGenerateViaGenerateRef.current = true
    }
  }, [projectIdFromQuery])

  useEffect(() => {
    refreshProjectKbContext()
  }, [projectIdFromQuery, refreshProjectKbContext, location.key])

  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === 'visible') refreshProjectKbContext()
    }
    window.addEventListener('focus', refreshProjectKbContext)
    document.addEventListener('visibilitychange', onVis)
    return () => {
      window.removeEventListener('focus', refreshProjectKbContext)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [refreshProjectKbContext])

  useEffect(() => {
    if (!projectIdFromQuery) {
      setExploded3dGlbObjectUrl('')
      setExploded3dPartDetails(null)
      return undefined
    }
    const urlRef = { current: '' }
    let cancelled = false
    loadExploded3dBundle(projectIdFromQuery)
      .then((b) => {
        if (cancelled || !b?.glbBlob) return
        const u = URL.createObjectURL(b.glbBlob)
        urlRef.current = u
        setExploded3dGlbObjectUrl(u)
        setExploded3dPartDetails(b.partDetails && typeof b.partDetails === 'object' ? b.partDetails : null)
      })
      .catch(() => {
        if (!cancelled) {
          setExploded3dGlbObjectUrl('')
          setExploded3dPartDetails(null)
        }
      })
    return () => {
      cancelled = true
      if (urlRef.current) URL.revokeObjectURL(urlRef.current)
      setExploded3dGlbObjectUrl('')
      setExploded3dPartDetails(null)
    }
  }, [projectIdFromQuery])

  const assistantEligibleFiles = useMemo(() => {
    const isVersionedReadyFile = (f) =>
      Boolean(f && String(f.fileVersionId || '').trim() && (f.kbImportComplete || f.status === 'done'))
    if (kbAllowList === null) return projectFiles.filter(isVersionedReadyFile)
    if (kbAllowList.length === 0) return []
    const allow = new Set(kbAllowList.map(String))
    return projectFiles.filter((f) => allow.has(String(f.id)) && isVersionedReadyFile(f))
  }, [projectFiles, kbAllowList])

  const selectedFileVersionIdsForAssistant = useMemo(() => {
    const eligibleById = new Map(
      (assistantEligibleFiles || []).map((f) => [String(f.id), String(f.fileVersionId || '').trim()]),
    )
    const ids = []
    for (const fid of Array.isArray(selectedSourceFiles) ? selectedSourceFiles : []) {
      const v = eligibleById.get(String(fid)) || ''
      if (v && !ids.includes(v)) ids.push(v)
    }
    return ids
  }, [assistantEligibleFiles, selectedSourceFiles])

  // 若用户已有非空故障树，但此前未记录“选源 file_version_id 基线签名”，在此对齐一次，避免后续误判知识库变更。
  useEffect(() => {
    if (graphData?.nodes?.length === 0) return
    if (kbSyncedBaselineFvSigRef.current) return
    const arr = (selectedFileVersionIdsForAssistant || []).map(String).filter(Boolean)
    const sig = Array.from(new Set(arr)).sort().join('|')
    if (sig) kbSyncedBaselineFvSigRef.current = sig
  }, [graphData?.nodes?.length, selectedFileVersionIdsForAssistant])

  useEffect(() => {
    /**
     * 注意：`assistantEligibleFiles` 可能在“刷新项目工作区/页面切换”的瞬间短暂变为空数组。
     * 若此时直接做交集，会把用户在助手中选好的依据文件意外清空。
     *
     * 只有在“明确约束选源范围”的情况下才强制收敛：
     * - kbAllowList 为空数组：用户明确在项目页取消了所有来源 => 允许清空
     * - 或 eligible 列表非空 => 正常做交集
     *
     * 其余情况（例如 projectFiles 尚未 hydrate 完成导致 eligible 暂为空）保持原选择不动。
     */
    const eligibleArr = Array.isArray(assistantEligibleFiles) ? assistantEligibleFiles : []
    const shouldAllowEmpty = Array.isArray(kbAllowList) && kbAllowList.length === 0
    if (!eligibleArr.length && !shouldAllowEmpty) return

    const eligible = new Set(eligibleArr.map((f) => String(f.id)))
    setSelectedSourceFiles((prev) => {
      const arr = Array.isArray(prev) ? prev.map(String) : []
      const next = arr.filter((id) => eligible.has(id))
      if (arr.length === next.length && arr.every((id, i) => id === next[i])) return prev
      if (arr.some((id) => !eligible.has(id))) {
        requireGenerateViaGenerateRef.current = true
      }
      return next
    })
  }, [assistantEligibleFiles, kbAllowList])

  const handleSelectSourceFiles = useCallback((next) => {
    const a = [...(Array.isArray(selectedSourceFiles) ? selectedSourceFiles : [])].map(String).sort()
    const b = [...(Array.isArray(next) ? next : [])].map(String).sort()
    if (a.join(',') !== b.join(',')) {
      requireGenerateViaGenerateRef.current = true
    }
    setSelectedSourceFiles(next)
  }, [selectedSourceFiles])

  // 修改任务已改为调用 fta-ai-service（不再使用前端模拟 patch）

  const upsertProgressMessage = useCallback(
    ({ taskId, agent = '调度器', avatar = 'S', quote = '', content = '', actions = null, actionsDisabled = false }) => {
      if (!taskId) return
      const msg = {
        id: `progress-${taskId}`,
        role: 'assistant',
        kind: 'progress',
        taskId,
        agent,
        avatar,
        quote,
        content,
        actions: Array.isArray(actions) ? actions : null,
        actionsDisabled: Boolean(actionsDisabled),
        at: Date.now(),
      }
      setAssistantMessages((prev) => {
        const next = []
        let replaced = false
        for (const m of prev) {
          if (m?.kind === 'progress' && m?.taskId === taskId) {
            if (!replaced) next.push(msg)
            replaced = true
          } else {
            next.push(m)
          }
        }
        if (!replaced) next.push(msg)
        return next.slice(-320)
      })
    },
    [],
  )

  /** 顶事件候选确认：taskId -> { candidates, continueWithCandidate(cand) } */
  const pendingTopEventConfirmRef = useRef(new Map())

  // 兼容极端情况下（HMR/打包作用域）进度卡片按钮闭包取不到 ref：挂到 window 兜底
  useEffect(() => {
    try {
      window.__fta_pendingTopEventConfirmRef = pendingTopEventConfirmRef
      window.__fta_upsertProgressMessage = upsertProgressMessage
    } catch {
      // ignore
    }
  }, [])

  const runGenerateTaskFromEmptyCanvas = useCallback(
    async (prompt, options = {}) => {
      const now = Date.now()
      const taskId = `task-${now}-${Math.random().toString(36).slice(2, 9)}`
      const promptPreview = prompt.length > 80 ? `${prompt.slice(0, 80)}…` : prompt

      setAssistantTasks((prev) => [
        {
          id: taskId,
          createdAt: now,
          userPrompt: prompt,
          promptPreview,
          title: '生成故障树',
          status: 'queued',
          progress: 0,
          stage: 'queued',
          message: '排队中',
          error: null,
          faultTreeId: null,
          jobId: null,
          itemId: null,
        },
        ...prev,
      ].slice(0, 80))

      assistantTaskEventHistoryRef.current.set(taskId, [])
      upsertProgressMessage({
        taskId,
        agent: '调度器',
        avatar: 'S',
        quote: promptPreview,
        content: '任务已提交，等待执行…',
      })

      const pushEvent = (evt) => {
        const prev = assistantTaskEventHistoryRef.current.get(taskId) || []
        assistantTaskEventHistoryRef.current.set(taskId, [...prev, evt].slice(-400))
      }

      /** 与后端 job-item.events 的 seq 错开，避免与 Mongo 递增 seq 混排时撞号 */
      let nextLocalEventSeq = 1_000_000
      const tick = (progress, agent, text) => {
        nextLocalEventSeq += 1
        setAssistantTasks((prev) =>
          prev.map((t) =>
            t.id === taskId
              ? {
                  ...t,
                  status: progress >= 100 ? 'completed' : 'running',
                  progress,
                  stage: 'running',
                  message: text,
                }
              : t,
          ),
        )
        const evt = {
          seq: nextLocalEventSeq,
          ts: new Date().toISOString(),
          agent,
          avatar: agent?.slice(0, 1) || 'A',
          level: 'INFO',
          text,
          progress,
        }
        pushEvent(evt)
        upsertProgressMessage({
          taskId,
          agent,
          avatar: evt.avatar,
          quote: promptPreview,
          content: `进度：${text}`,
        })
      }

      /** 轮询时只刷新任务行与对话气泡：后端 events 已含详细步骤，勿再写入历史（否则会与每轮 tick 重复） */
      const syncPollProgressUi = (progress, text) => {
        setAssistantTasks((prev) =>
          prev.map((t) =>
            t.id === taskId
              ? {
                  ...t,
                  status: progress >= 100 ? 'completed' : 'running',
                  progress,
                  stage: 'running',
                  message: text,
                }
              : t,
          ),
        )
        upsertProgressMessage({
          taskId,
          agent: '调度器',
          avatar: 'S',
          quote: promptPreview,
          content: `进度：${text}`,
        })
      }

      tick(8, '调度器', '正在连接后端…')
      const doGenerate = async (opts = {}) => {
        if (!selectedFileVersionIdsForAssistant.length) {
          const err = '请先在右侧「依据文件」中至少选择 1 个已导入完成的文件（file_version_id）'
          throw new Error(err)
        }
        const payload = { prompt: opts.promptOverride || prompt, ...opts }
        delete payload.promptOverride
        payload.selectedFileVersionIds = selectedFileVersionIdsForAssistant
        const hasExploded3dForPrompt =
          !!exploded3dGlbObjectUrl &&
          exploded3dPartDetails &&
          typeof exploded3dPartDetails === 'object' &&
          Object.keys(exploded3dPartDetails).length > 0
        if (hasExploded3dForPrompt) {
          payload.partDetails = exploded3dPartDetails
        }
        const r = normalizeGenerateResponse(await generateTree(payload))
        return r
      }

      const continueWithResponse = async (resp) => {
        const hasInlineTree =
          resp?.tree_id &&
          (resp?.tree_data || resp?.treeData) &&
          typeof (resp.tree_data || resp.treeData) === 'object' &&
          (Array.isArray((resp.tree_data || resp.treeData).nodeList) ||
            Array.isArray((resp.tree_data || resp.treeData).linkList))

        const mergeJobItemEventsIntoTask = async (itemId) => {
          if (!itemId) return
          try {
            const item = await getBatchJobItem({ itemId })
            mergeBackendJobEventsIntoTaskRef(assistantTaskEventHistoryRef, taskId, item, () =>
              setAssistantTaskEventsRevision((n) => n + 1),
            )
          } catch (err) {
            console.warn('getBatchJobItem events', err)
          }
        }

        if (hasInlineTree) {
          const verPayload = buildVersionPayloadFromGenerateResponse(resp)
          if (!verPayload) {
            const err = '生成响应中 tree_data 无效'
            setAssistantTasks((prev) =>
              prev.map((t) => (t.id === taskId ? { ...t, status: 'failed', error: err } : t)),
            )
            upsertProgressMessage({
              taskId,
              agent: '流程控制',
              avatar: 'P',
              quote: promptPreview,
              content: `错误：${err}`,
            })
            return { taskId, error: err }
          }
          try {
            applyBackendVersionPayload(verPayload)
          } catch (err) {
            const msg = err?.message || String(err)
            setAssistantTasks((prev) =>
              prev.map((t) => (t.id === taskId ? { ...t, status: 'failed', error: msg } : t)),
            )
            upsertProgressMessage({
              taskId,
              agent: '流程控制',
              avatar: 'P',
              quote: promptPreview,
              content: `错误：${msg}`,
            })
            return { taskId, error: msg }
          }
          setBackendTreeId(String(resp.tree_id))
          setAssistantTasks((prev) =>
            prev.map((t) =>
              t.id === taskId
                ? {
                    ...t,
                    status: 'completed',
                    progress: 100,
                    stage: resp.mode === 'reuse' ? 'reuse' : 'completed',
                    message: resp.mode === 'reuse' ? '已复用故障树' : '生成完成',
                    faultTreeId: resp.tree_id,
                    itemId: resp.item_id || null,
                    jobId: resp.job_id || null,
                  }
                : t,
            ),
          )
          await mergeJobItemEventsIntoTask(resp.item_id)
          setAssistantTasks((prev) => [...prev])
          tick(
            100,
            '流程控制',
            resp.mode === 'reuse'
              ? `已复用（tree_id=${resp.tree_id}）`
              : `生成完成（tree_id=${resp.tree_id}）`,
          )
          upsertProgressMessage({
            taskId,
            agent: '流程控制',
            avatar: 'P',
            quote: promptPreview,
            content:
              resp.mode === 'reuse'
                ? `已复用故障树并加载到画布（tree_id=${resp.tree_id}）。`
                : `生成完成并加载到画布（tree_id=${resp.tree_id}）。可在「进度详情」查看多智能体执行轨迹。`,
          })
          return { taskId, treeId: resp.tree_id, reuse: resp.mode === 'reuse' }
        }

        if (resp?.mode === 'reuse' && resp?.tree_id) {
          tick(100, '流程控制', `已复用故障树（tree_id=${resp.tree_id}）`)
          setBackendTreeId(String(resp.tree_id))
          const ver = await getTree({ treeId: resp.tree_id })
          applyBackendVersionPayload(ver)
          setAssistantTasks((prev) =>
            prev.map((t) =>
              t.id === taskId
                ? {
                    ...t,
                    status: 'completed',
                    progress: 100,
                    stage: 'reuse',
                    faultTreeId: resp.tree_id,
                    itemId: resp.item_id || null,
                    jobId: resp.job_id || null,
                  }
                : t,
            ),
          )
          await mergeJobItemEventsIntoTask(resp.item_id)
          setAssistantTasks((prev) => [...prev])
          return { taskId, treeId: resp.tree_id, reuse: true }
        }

        if (resp?.mode === 'queued' && resp?.item_id) {
          setAssistantTasks((prev) =>
            prev.map((t) =>
              t.id === taskId ? { ...t, itemId: resp.item_id, jobId: resp.job_id || null } : t,
            ),
          )
          const finalItem = await pollGenerationJobItem({
            itemId: resp.item_id,
            onUpdate: (it) => {
              const p = Math.max(0, Math.min(100, Number(it?.progress) || 0))
              syncPollProgressUi(p, it?.message || '生成中…')
              mergeBackendJobEventsIntoTaskRef(assistantTaskEventHistoryRef, taskId, it, () =>
                setAssistantTaskEventsRevision((n) => n + 1),
              )
            },
          })
          if (finalItem?.status === 'success' && finalItem?.tree_id) {
            tick(100, '流程控制', `生成完成（tree_id=${finalItem.tree_id}）`)
            setBackendTreeId(String(finalItem.tree_id))
            const ver = await getTree({ treeId: finalItem.tree_id })
            applyBackendVersionPayload(ver)
            await mergeJobItemEventsIntoTask(resp.item_id)
            setAssistantTasks((prev) => [...prev])
            return { taskId, treeId: finalItem.tree_id, reuse: false }
          }
          setAssistantTasks((prev) =>
            prev.map((t) =>
              t.id === taskId ? { ...t, status: 'failed', error: finalItem?.error || '生成失败' } : t,
            ),
          )
          upsertProgressMessage({
            taskId,
            agent: '流程控制',
            avatar: 'P',
            quote: promptPreview,
            content: `错误：${finalItem?.error || '生成失败'}`,
          })
          return { taskId, error: finalItem?.error || '生成失败' }
        }

        const parseHint = (() => {
          try {
            const keys = Object.keys(resp || {}).slice(0, 18).join(', ')
            return keys ? `（响应字段：${keys}）` : ''
          } catch {
            return ''
          }
        })()
        setAssistantTasks((prev) =>
          prev.map((t) =>
            t.id === taskId ? { ...t, status: 'failed', error: `无法解析后端响应${parseHint}` } : t,
          ),
        )
        return { taskId, error: `无法解析后端响应${parseHint}` }
      }

      let resp
      try {
        resp = options?.initialResponse ? normalizeGenerateResponse(options.initialResponse) : await doGenerate()
      } catch (e) {
        const msg = formatGenerateBackendError(e?.message || String(e))
        setAssistantTasks((prev) =>
          prev.map((t) => (t.id === taskId ? { ...t, status: 'failed', error: msg } : t)),
        )
        upsertProgressMessage({
          taskId,
          agent: '流程控制',
          avatar: 'P',
          quote: promptPreview,
          content: `错误：${msg}`,
        })
        return { taskId, error: msg }
      }

      // 新版后端：顶事件未精确命中时需要用户确认候选
      if (resp?.mode === 'need_confirmation' && Array.isArray(resp?.candidates) && resp.candidates.length > 0) {
        const candidates = resp.candidates

        setAssistantTasks((prev) =>
          prev.map((t) =>
            t.id === taskId
              ? { ...t, status: 'queued', progress: 12, stage: 'need_confirmation', message: '等待用户确认顶事件' }
              : t,
          ),
        )

        // 注册 continuation：用户点击候选后继续生成
        pendingTopEventConfirmRef.current.set(taskId, {
          candidates,
          continueWithCandidate: async (cand) => {
            const disp = cand?.display_name || cand?.name || cand?.normalized_top_event || ''
            tick(14, '调度器', `已选择顶事件：${disp}，正在提交生成任务…`)

            // 用“用户选择的顶事件名”构造新 prompt，确保后端按该顶事件解析生成
            const reqTop = String(cand?.name || cand?.display_name || cand?.normalized_top_event || '').trim()
            const req = String(resp?.requirements || resp?.parsed_prompt?.requirements || '').trim()
            const promptOverride = reqTop
              ? `请生成一棵顶事件为“${reqTop}”的故障树。${req ? `\n要求：${req}` : ''}`
              : undefined
            try {
              const next = await doGenerate({
                ...(promptOverride ? { promptOverride } : null),
                confirmedTopEvent: cand?.name || cand?.display_name || '',
                confirmedNormalizedTopEvent: cand?.normalized_top_event || cand?.normalized_name || '',
                confirmedGraphNodeId: cand?.graph_node_id || '',
                selectedFileVersionIds: resp?.selected_file_version_ids || [],
              })
              return await continueWithResponse(next)
            } catch (err) {
              const msg = formatGenerateBackendError(err?.message || String(err))
              setAssistantTasks((prev) =>
                prev.map((t) => (t.id === taskId ? { ...t, status: 'failed', error: msg } : t)),
              )
              upsertProgressMessage({
                taskId,
                agent: '流程控制',
                avatar: 'P',
                quote: promptPreview,
                content: `错误：${msg}`,
              })
              setAssistantMessages((prev) =>
                prev.concat({
                  id: `a-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
                  role: 'assistant',
                  kind: 'text',
                  content: `生成失败：${msg}`,
                  at: Date.now(),
                }),
              )
              throw err
            }
          },
        })

        upsertProgressMessage({
          taskId,
          agent: '调度器',
          avatar: 'S',
          quote: promptPreview,
          content: '顶事件未精确命中：请在下方选择一个最接近的顶事件候选，以继续生成。',
          actions: candidates.map((c) => ({
            kind: 'top_event_candidate',
            label: c?.display_name || c?.name || c?.normalized_top_event || '—',
            candidate: c,
          })),
        })
        return { taskId, needConfirmation: true }
      }

      return await continueWithResponse(resp)
    },
    [
      applyBackendVersionPayload,
      upsertProgressMessage,
      setBackendTreeId,
      selectedFileVersionIdsForAssistant,
      exploded3dGlbObjectUrl,
      exploded3dPartDetails,
    ],
  )

  const handleAssistantSend = useCallback(async () => {
    const text = assistantInput.trim()
    if (!text) return
    const now = Date.now()
    const wasEmptyCanvas = graphData.nodes.length === 0
    const textLooksLikeGenerate = /生成|构建|新建|重新生成|重建|画一棵|创建|generate|create/i.test(text)
    const textLooksLikeHelp = /你好|您好|介绍一下|你能做什么|能做什么|帮助|怎么用|怎么说|如何表达|示例|例子|举例|模板|格式|hello|hi|help/i.test(text)

    const wsEpoch =
      projectIdFromQuery ? Number(getWorkspace(projectIdFromQuery)?.kbDatasetEpoch) || 0 : 0
    const currentFvSig = (() => {
      const arr = (selectedFileVersionIdsForAssistant || []).map(String).filter(Boolean)
      // 以“集合”语义对齐：去重 + 排序，避免顺序变化导致误判“知识库变更”
      const uniq = Array.from(new Set(arr)).sort()
      return uniq.join('|')
    })()
    const baselineFvSig = (() => {
      const raw = String(kbSyncedBaselineFvSigRef.current || '')
      if (!raw) return ''
      // 兼容历史存储：若曾按未排序方式 join，这里也归一化为集合签名
      const uniq = Array.from(new Set(raw.split('|').map((s) => String(s || '').trim()).filter(Boolean))).sort()
      return uniq.join('|')
    })()
    const mustUseGeneratePipeline =
      !wasEmptyCanvas &&
      (requireGenerateViaGenerateRef.current || wsEpoch > kbSyncedBaselineEpochRef.current) &&
      currentFvSig !== baselineFvSig

    const rollbackSnapId = `snap-pre-${now}-${Math.random().toString(36).slice(2, 8)}`
    let preGraph
    try {
      preGraph = JSON.parse(JSON.stringify(graphData))
    } catch {
      preGraph = { nodes: [...(graphData.nodes || [])], edges: [...(graphData.edges || [])] }
    }
    const preRaw = rawJsonText

    setAssistantSnapshots((prev) =>
      [...prev, { id: rollbackSnapId, at: now, title: '对话前', graphData: preGraph, rawJsonText: preRaw }].slice(
        -120,
      ),
    )
    const userMsg = {
      id: `u-${now}`,
      role: 'user',
      kind: 'text',
      content: text,
      at: now,
      rollbackSnapshotId: rollbackSnapId,
    }
    setAssistantMessages((prev) => [...prev, userMsg].slice(-320))
    setAssistantInput('')

    setAssistantBusy(true)
    const thinkingMsgId = `t-${now}`
    setAssistantMessages((prev) => [
      ...prev,
      {
        id: thinkingMsgId,
        role: 'assistant',
        kind: 'thinking',
        content: '正在理解你的请求…',
        at: Date.now(),
      },
    ])
    try {
      const selectedFilesForAgent = (selectedSourceFiles || []).map((id) => {
        const f =
          assistantEligibleFiles.find((x) => String(x.id) === String(id)) ||
          projectFiles.find((x) => String(x.id) === String(id))
        return { id, name: f?.name || id, file_version_id: f?.fileVersionId || '' }
      })
      const baselineFileVersionIdsForAgent = baselineFvSig
        ? baselineFvSig.split('|').map((s) => String(s || '').trim()).filter(Boolean)
        : []
      let assistantAgentResp = null
      try {
        assistantAgentResp = await sendAssistantAgentMessage({
          sessionId: assistantStoreKey,
          projectId: projectIdFromQuery,
          canvasId: canvasIdFromQuery || treeIdFromQuery || backendTreeId || '',
          message: text,
          currentTree: safeJsonParse(rawJsonText, graphData),
          currentTreeId: backendTreeId || treeIdFromQuery || '',
          selectedFileVersionIds: selectedFileVersionIdsForAssistant,
          baselineFileVersionIds: baselineFileVersionIdsForAgent,
          selectedFiles: selectedFilesForAgent,
          workspaceKbEpoch: wsEpoch,
          forceGenerate: mustUseGeneratePipeline,
          frontendMessageId: userMsg.id,
          frontendContext: {
            wasEmptyCanvas,
            mustUseGeneratePipeline,
            requireGenerateViaGenerate: requireGenerateViaGenerateRef.current,
            hasPending: Boolean(assistantPending),
          },
        })
      } catch (agentErr) {
        console.warn('AssistantAgent unavailable, falling back to legacy keyword route:', agentErr)
      }

      if (assistantAgentResp) {
        const action = String(assistantAgentResp.action || '')
        const intent = String(assistantAgentResp.intent || '')
        if (action === 'chat' || action === 'need_source_files' || action === 'validation_finished' || action === 'need_current_tree') {
          setAssistantMessages((prev) =>
            prev
              .filter((m) => m.id !== thinkingMsgId)
              .concat({
                id: `a-${Date.now()}`,
                role: 'assistant',
                kind: action === 'validation_finished' ? 'patch' : 'text',
                content: assistantAgentResp.assistant_message || 'AssistantAgent 已处理请求。',
                at: Date.now(),
              })
              .slice(-320),
          )
          return
        }

        if (intent === 'generate_tree' || intent === 'regenerate_tree' || action.startsWith('generation_') || action === 'need_top_event_confirmation') {
          if (mustUseGeneratePipeline) requireGenerateViaGenerateRef.current = false
          const gen = await runGenerateTaskFromEmptyCanvas(text, { initialResponse: assistantAgentResp.result || {} })
          setAssistantMessages((prev) =>
            prev.filter((m) => m.id !== thinkingMsgId).concat({
              id: `a-${Date.now()}`,
              role: 'assistant',
              kind: 'text',
              content: gen?.needConfirmation
                ? 'AssistantAgent 已找到多个相似顶事件候选。请在上方进度卡片下方选择一个候选，以继续生成。'
                : gen?.treeId
                  ? assistantAgentResp.assistant_message || `已生成故障树并加载到画布（tree_id=${gen.treeId}）。`
                  : `生成失败：${gen?.error || '未知错误'}`,
              at: Date.now(),
            }),
          )
          if (gen?.needConfirmation) {
            if (mustUseGeneratePipeline) requireGenerateViaGenerateRef.current = true
            return
          }
          if (gen?.treeId) {
            setAssistantPending({
              kind: 'generate',
              backendTreeId: gen.treeId,
              deleteOnUndo: gen.reuse !== true,
              prev: { graphData: preGraph, rawJsonText: preRaw },
            })
            kbSyncedBaselineEpochRef.current = wsEpoch
            kbSyncedBaselineFvSigRef.current = currentFvSig
            requireGenerateViaGenerateRef.current = false
          } else if (mustUseGeneratePipeline) {
            requireGenerateViaGenerateRef.current = true
          }
          return
        }

        if (action === 'edit_draft_created') {
          const prevGraph = graphData
          const prevRaw = rawJsonText
          const updated = assistantAgentResp?.result?.updated_tree_json
          if (!updated) throw new Error('AssistantAgent 未返回 updated_tree_json')

          const nextGraph = normalizeToGraph(updated)
          const diff = computeGraphDiff(prevGraph, nextGraph)
          const { pendingGraph: decorated, finalGraph } = buildPendingAiEditGraph(
            prevGraph,
            nextGraph,
            diff,
          )

          applyEdit(decorated)
          setAssistantPending({
            kind: 'edit',
            prev: { graphData: prevGraph, rawJsonText: prevRaw },
            finalGraph,
            diff,
            at: Date.now(),
            pendingId: assistantAgentResp?.pending_action?.pending_id || '',
          })

          const snap = {
            id: `s-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
            at: Date.now(),
            title: text.length > 26 ? `${text.slice(0, 26)}…` : text,
            graphData: decorated,
            rawJsonText: prevRaw,
          }
          setAssistantSnapshots((prev) => [...prev, snap].slice(-120))

          const diffText = `新增 ${diff.added.length} · 修改 ${diff.modified.length} · 删除 ${diff.removed.length}`
          setAssistantMessages((prev) =>
            prev
              .filter((m) => m.id !== thinkingMsgId)
              .concat({
                id: `a-${Date.now()}`,
                role: 'assistant',
                kind: 'patch',
                content: `${assistantAgentResp.assistant_message || diffText}\n已在画布中标注：新增的节点为绿色、修改的节点为黄色、删除的节点为红色。`,
                at: Date.now(),
                snapshotId: snap.id,
                diff,
              })
              .slice(-320),
          )
          return
        }

        setAssistantMessages((prev) =>
          prev
            .filter((m) => m.id !== thinkingMsgId)
            .concat({
              id: `a-${Date.now()}`,
              role: 'assistant',
              kind: 'text',
              content: assistantAgentResp.assistant_message || 'AssistantAgent 已处理请求。',
              at: Date.now(),
            })
            .slice(-320),
        )
        return
      }

      // 规则：如果当前画布为空 => 视为“生成一棵故障树”的任务（走 FTA-Latest 并生成任务/进度/泳道图）
      if (wasEmptyCanvas && textLooksLikeGenerate && !textLooksLikeHelp) {
        const gen = await runGenerateTaskFromEmptyCanvas(text)
        setAssistantMessages((prev) =>
          prev.filter((m) => m.id !== thinkingMsgId).concat({
            id: `a-${Date.now()}`,
            role: 'assistant',
            kind: 'text',
            content: gen?.needConfirmation
              ? '已找到多个相似顶事件候选。请在上方进度卡片下方选择一个候选，以继续生成。'
              : gen?.treeId
                ? `已生成故障树并加载到画布（tree_id=${gen.treeId}）。`
                : `生成失败：${gen?.error || '未知错误'}`,
            at: Date.now(),
          }),
        )
        if (gen?.needConfirmation) {
          return
        }
        if (gen?.treeId) {
          setAssistantPending({
            kind: 'generate',
            backendTreeId: gen.treeId,
            deleteOnUndo: gen.reuse !== true,
            prev: { graphData: preGraph, rawJsonText: preRaw },
          })
          // 关键：首次生成成功后即与项目页知识库 epoch 对齐，否则下一条编辑消息会误判“知识库已变更”而强制走 generate。
          kbSyncedBaselineEpochRef.current = wsEpoch
          kbSyncedBaselineFvSigRef.current = currentFvSig
          requireGenerateViaGenerateRef.current = false
        }
        return
      }

      if (wasEmptyCanvas) {
        setAssistantMessages((prev) =>
          prev
            .filter((m) => m.id !== thinkingMsgId)
            .concat({
              id: `a-${Date.now()}`,
              role: 'assistant',
              kind: 'text',
              content:
                '当前画布还没有故障树。我可以帮你生成、编辑、校验故障树，也可以解释如何描述生成需求。要开始生成时，请明确说“为某个顶事件生成故障树”。',
              at: Date.now(),
            })
            .slice(-320),
        )
        return
      }

      if (mustUseGeneratePipeline) {
        requireGenerateViaGenerateRef.current = false
        const gen = await runGenerateTaskFromEmptyCanvas(text)
        setAssistantMessages((prev) =>
          prev.filter((m) => m.id !== thinkingMsgId).concat({
            id: `a-${Date.now()}`,
            role: 'assistant',
            kind: 'text',
            content: gen?.needConfirmation
              ? '已找到多个相似顶事件候选。请在上方进度卡片下方选择一个候选，以继续生成。'
              : gen?.treeId
                ? `已根据最新知识库来源重新生成并加载故障树（tree_id=${gen.treeId}）。`
                : `生成失败：${gen?.error || '未知错误'}`,
            at: Date.now(),
          }),
        )
        if (gen?.needConfirmation) {
          requireGenerateViaGenerateRef.current = true
          return
        }
        if (gen?.treeId) {
          setAssistantPending({
            kind: 'generate',
            backendTreeId: gen.treeId,
            deleteOnUndo: gen.reuse !== true,
            prev: { graphData: preGraph, rawJsonText: preRaw },
          })
          kbSyncedBaselineEpochRef.current = wsEpoch
          kbSyncedBaselineFvSigRef.current = currentFvSig
        } else {
          requireGenerateViaGenerateRef.current = true
        }
        return
      }

      // 否则：视为编辑现有故障树（只调用 fta-ai-service；不生成任务、不展示泳道图）
      const prevGraph = graphData
      const prevRaw = rawJsonText
      const payload = safeJsonParse(rawJsonText, null)
      const editResp = await editFaultTreeWithAi({
        instruction: text,
        treeJson: payload,
        selectedFiles: (selectedSourceFiles || []).map((id) => {
          const f =
            assistantEligibleFiles.find((x) => String(x.id) === String(id)) ||
            projectFiles.find((x) => String(x.id) === String(id))
          return { id, name: f?.name || id }
        }),
      })

      const updated = editResp?.updated_tree_json
      if (!updated) throw new Error('AI 编辑服务未返回 updated_tree_json')

      const nextGraph = normalizeToGraph(updated)
      const diff = computeGraphDiff(prevGraph, nextGraph)
      const { pendingGraph: decorated, finalGraph } = buildPendingAiEditGraph(
        prevGraph,
        nextGraph,
        diff,
      )

      applyEdit(decorated)
      setAssistantPending({
        kind: 'edit',
        prev: { graphData: prevGraph, rawJsonText: prevRaw },
        finalGraph,
        diff,
        at: Date.now(),
      })

      const snap = {
        id: `s-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        at: Date.now(),
        title: text.length > 26 ? `${text.slice(0, 26)}…` : text,
        graphData: decorated,
        rawJsonText: prevRaw,
      }
      setAssistantSnapshots((prev) => [...prev, snap].slice(-120))

      const diffText = `新增 ${diff.added.length} · 修改 ${diff.modified.length} · 删除 ${diff.removed.length}`
      setAssistantMessages((prev) =>
        prev
          .filter((m) => m.id !== thinkingMsgId)
          .concat({
            id: `a-${Date.now()}`,
            role: 'assistant',
            kind: 'patch',
            content: `${diffText}\n已在画布中标注：新增的节点为绿色、修改的节点为黄色、删除的节点为红色。`,
            at: Date.now(),
            snapshotId: snap.id,
            diff,
          })
          .slice(-320),
      )
    } catch (e) {
      if (mustUseGeneratePipeline && !wasEmptyCanvas) {
        requireGenerateViaGenerateRef.current = true
      }
      const errText = formatGenerateBackendError(e?.message || String(e))
      setAssistantMessages((prev) =>
        prev
          .filter((m) => m.id !== thinkingMsgId)
          .concat({
            id: `a-${Date.now()}`,
            role: 'assistant',
            kind: 'error',
            content: `生成失败：${errText}`,
            at: Date.now(),
          })
          .slice(-320),
      )
    } finally {
      setAssistantBusy(false)
    }
  }, [
    assistantInput,
    graphData,
    runGenerateTaskFromEmptyCanvas,
    rawJsonText,
    selectedSourceFiles,
    projectFiles,
    assistantEligibleFiles,
    selectedFileVersionIdsForAssistant,
    assistantStoreKey,
    canvasIdFromQuery,
    treeIdFromQuery,
    backendTreeId,
    assistantPending,
    applyEdit,
    projectIdFromQuery,
  ])

  const handleAssistantAccept = useCallback(() => {
    if (!assistantPending) return
    if (assistantPending.kind === 'generate') {
      setAssistantPending(null)
      return
    }
    const finalGraph = assistantPending.finalGraph || graphData
    const cleared = clearDiffFromGraph(finalGraph)
    setGraphData(cleared)
    setRawJsonText(syncRawFromGraph(cleared))
    setAssistantPending(null)
  }, [assistantPending, graphData, syncRawFromGraph])

  const handleAssistantUndo = useCallback(async () => {
    if (!assistantPending?.prev) return
    const p = assistantPending
    if (p.kind === 'generate') {
      const backupRaw = rawJsonText
      const backupGraph = graphData
      if (p.deleteOnUndo && p.backendTreeId) {
        try {
          await deleteTree({ treeId: p.backendTreeId })
        } catch (e) {
          setError(String(e?.message || '撤销时删除后端故障树失败'))
          return
        }
      }
      setAssistantSnapshots((prev) =>
        [
          ...prev,
          {
            id: `arch-gen-${Date.now()}`,
            at: Date.now(),
            title: p.backendTreeId ? `已撤销生成的树（${p.backendTreeId}）` : '已撤销生成',
            graphData: backupGraph,
            rawJsonText: backupRaw,
          },
        ].slice(-120),
      )
      setBackendTreeId('')
      restoreSnapshot(p.prev)
      setAssistantPending(null)
      return
    }
    restoreSnapshot(p.prev)
    setAssistantPending(null)
  }, [assistantPending, restoreSnapshot, rawJsonText, graphData])

  /** 回溯到某条用户消息之前：恢复画布、截断后续对话、原文填入输入框 */
  const handleRollbackUserMessage = useCallback(
    async (userMsgId) => {
      const idx = assistantMessages.findIndex((m) => m.id === userMsgId)
      if (idx < 0) return
      const msg = assistantMessages[idx]
      try {
        await truncateAssistantSession({
          sessionId: assistantStoreKey,
          frontendMessageId: userMsgId,
          keepBeforeIndex: idx,
        })
      } catch (err) {
        console.warn('truncate AssistantAgent session failed:', err)
      }
      const snapId = msg.rollbackSnapshotId
      if (snapId) {
        const snap = assistantSnapshots.find((s) => s.id === snapId)
        if (snap) restoreSnapshot(snap)
      }
      setAssistantInput(String(msg.content || ''))
      setAssistantPending(null)
      setAssistantMessages((prev) => prev.slice(0, idx))
    },
    [assistantMessages, assistantSnapshots, restoreSnapshot, assistantStoreKey],
  )

  // 手动触发“逻辑校验”（规则引擎，来自 validator-service `/validate-fault-tree`）
  const runRuleValidation = useCallback(async () => {
    if (!graphData || graphData.nodes.length === 0) return null
    setValidationLoading(true)
    setValidationError('')
    try {
      const res = await validateFaultTreeGraph({
        graph: { nodes: graphData.nodes || [], edges: graphData.edges || [] },
      })
      const v = res?.validation || null
      setValidation(v)
      return v
    } catch (err) {
      if (err?.name === 'AbortError') return null
      const msg = String(err?.message || '')
      if (msg.toLowerCase().includes('aborted')) return null
      console.error(err)
      setValidationError(err?.message || '调用校验服务失败')
      setValidation(null)
      return null
    } finally {
      setValidationLoading(false)
    }
  }, [graphData])

  const runSemanticValidation = useCallback(async () => {
    setAiSemanticLoading(true)
    setAiSemanticError('')
    setAiSemanticValidation(null)
    setAiSemanticNotice('')
    try {
      const treeData = extractTreeDataForBackend({ rawJsonText, graphData, parsedInfo })
      const res = await validateTreeSemantic({ treeData })
      setAiSemanticValidation(res || null)
      // 记录本次校验对应的“画布签名”，用于判断后续是否过期
      if (editSignature) setAiSemanticLastSig(editSignature)
      return res || null
    } catch (e) {
      console.error(e)
      setAiSemanticError(e?.message || '调用 AI 语义校验失败')
      setAiSemanticValidation(null)
      return null
    } finally {
      setAiSemanticLoading(false)
    }
  }, [graphData, parsedInfo, rawJsonText, editSignature])

  const handleCheck = useCallback(async () => {
    setError('')
    setNotice('')
    setShowAiPanel(true)
    // 先刷新一次规则校验
    const v = await runRuleValidation()
    if (!v || v.error_count > 0) return
    // 若没有任何新修改，则提示用户无需重复校验
    if (aiSemanticLastSig && editSignature && aiSemanticLastSig === editSignature) {
      setShowAiPanel(true)
      setAiSemanticNotice('没有新的修改。')
      return
    }
    // 规则通过且有新修改后自动进行 AI 语义校验，并在面板中显示
    await runSemanticValidation()
  }, [runRuleValidation, runSemanticValidation, aiSemanticLastSig, editSignature])

  // 逻辑校验（规则引擎）：调用合并后的 validator-service `/validate-fault-tree`
  // 该接口只做结构规则校验，不调大模型，可在编辑过程中实时刷新。
  useEffect(() => {
    if (!graphData || graphData.nodes.length === 0) {
      setValidation(null)
      setValidationError('')
      return
    }

    setValidationLoading(true)
    setValidationError('')

    const controller = new AbortController()
    const timer = setTimeout(async () => {
      try {
        const res = await validateFaultTreeGraph({
          graph: { nodes: graphData.nodes || [], edges: graphData.edges || [] },
          signal: controller.signal,
        })
        setValidation(res?.validation || null)
      } catch (err) {
        if (err?.name === 'AbortError') return
        const msg = String(err?.message || '')
        if (msg.toLowerCase().includes('aborted')) return
        console.error(err)
        setValidationError(err?.message || '调用校验服务失败')
        setValidation(null)
      } finally {
        setValidationLoading(false)
      }
    }, 350)

    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [graphData])

  useEffect(() => {
    if (!treeIdFromQuery) return
    const controller = new AbortController()
    setBackendLoading(true)
    setError('')
    setNotice('')
    ;(async () => {
      try {
        const ver = await getTree({ treeId: treeIdFromQuery, signal: controller.signal })
        if (controller.signal.aborted) return
        setBackendTreeId(treeIdFromQuery)
        applyBackendVersionPayload(ver)
      } catch (e) {
        if (e?.name === 'AbortError') return
        const msg = String(e?.message || '')
        if (msg.toLowerCase().includes('aborted')) return
        setError(e?.message || '从后端加载故障树失败')
      } finally {
        if (!controller.signal.aborted) setBackendLoading(false)
      }
    })()

    return () => controller.abort()
  }, [treeIdFromQuery, applyBackendVersionPayload])

  /** 任意方式关联到后端 tree 后拉取版本列表（含：URL 打开、AI 生成、本地草稿恢复） */
  useEffect(() => {
    if (!backendTreeId) {
      setVersionList([])
      return
    }
    const controller = new AbortController()
    setHistoryLoading(true)
    setHistoryError('')
    ;(async () => {
      try {
        const hist = await getTreeHistory({ treeId: backendTreeId, signal: controller.signal })
        if (controller.signal.aborted) return
        setVersionList(Array.isArray(hist) ? hist : [])
      } catch (e) {
        if (e?.name === 'AbortError') return
        console.error(e)
        setHistoryError(e?.message || '加载版本列表失败')
        setVersionList([])
      } finally {
        if (!controller.signal.aborted) setHistoryLoading(false)
      }
    })()
    return () => controller.abort()
  }, [backendTreeId])

  const handleJsonChange = useCallback((e) => {
    setRawJsonText(e.target.value)
  }, [])

  const handleApplyJson = useCallback(() => {
    try {
      const parsed = JSON.parse(rawJsonText)
      const { trimmed, logic, ai } = splitEmbeddedValidation(parsed)
      const normalized = normalizeToGraph(trimmed)
      setHistory((h) => [...h, { graphData, rawJsonText }])
      setRedoHistory([])
      setGraphData(normalized)
      setRawJsonText(JSON.stringify(trimmed, null, 2))
      if (logic) setValidation(logic)
      if (ai) setAiEmbeddedValidation(ai)
      setError('')
    } catch (err) {
      setError(`JSON 解析失败：${err.message}`)
    }
  }, [rawJsonText, graphData, rawJsonText])

  const handleDownloadJson = useCallback(() => {
    const blob = new Blob([rawJsonText], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'fault-tree.json'
    a.click()
    URL.revokeObjectURL(url)
  }, [rawJsonText])

  const handleDownloadImage = useCallback(async () => {
    if (!canvasActionsRef.current?.exportImage) return
    try {
      setExporting(true)
      await new Promise((r) => setTimeout(r, 50))
      const dataUrl = await canvasActionsRef.current.exportImage()
      if (!dataUrl) throw new Error('export returned empty')
      const link = document.createElement('a')
      link.href = dataUrl
      link.download = 'fault-tree.png'
      link.click()
    } catch (err) {
      console.error(err)
      setError('图片导出失败，请重试')
    } finally {
      setExporting(false)
    }
  }, [])

  const handleDownloadHiResImage = useCallback(async () => {
    try {
      // 高保真导出使用“修剪后的 snapshot”，避免把 description/message 等无关字段塞进 URL 导致 431
      const snapParam = encodeURIComponent(hiResSnapshotText)
      const viewerPath = `/fta-viewer?snapshot=${snapParam}`
      const backendBaseUrl = getFtaBackendBaseUrl()
      const resp = await fetch(`${backendBaseUrl}/export-fault-tree-image`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: viewerPath,
          selector: '.fta-canvas-wrapper',
          width: 1600,
          height: 900,
          scale: 3.0,
          theme,
        }),
      })
      if (!resp.ok) {
        let detail = ''
        try {
          detail = await resp.text()
        } catch {
          detail = ''
        }
        throw new Error(`高保真导出失败：${resp.status}${detail ? `\n${detail}` : ''}`)
      }
      const blob = await resp.blob()
      const dlUrl = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = dlUrl
      a.download = 'fault-tree-hires.png'
      a.click()
      URL.revokeObjectURL(dlUrl)
    } catch (e) {
      console.error(e)
      setError(e.message || '高保真图片导出失败')
    }
  }, [hiResSnapshotText, theme])

  const doSubmit = useCallback(async (descriptionInput) => {
    setError('')
    setNotice('')
    // 提交前强制刷新一次规则校验结果（validator-service）
    const v = await runRuleValidation()
    if (!v || v.error_count > 0) return

    if (!backendTreeId) {
      setNotice('校验通过。当前为本地演示数据（未关联后端 tree_id），无法保存到后端。')
      return
    }

    const desc = (descriptionInput || '').trim() || '前端保存'

    setAiLoading(true)
    setAiError('')
    setAiValidation(null)
    try {
      const treeData = extractTreeDataForBackend({ rawJsonText, graphData, parsedInfo })
      const resp = await saveTree({ treeId: backendTreeId, treeData, editor: '专家', description: desc })
      setBackendVersion(resp?.version ?? backendVersion)
      setAiValidation({
        suggestions: `已保存为版本 ${resp?.version ?? ''}。学习条目数：${resp?.learned_count ?? 0}`,
      })
      setNotice(`已保存到后端（tree_id=${backendTreeId}，version=${resp?.version ?? ''}）`)
      setSubmittedToBackend(true)
      setBaselineJsonText(canonicalJsonString(rawJsonText))
      try {
        const hist = await getTreeHistory({ treeId: backendTreeId })
        setVersionList(Array.isArray(hist) ? hist : [])
        const vnum = resp?.version
        if (vnum != null && Array.isArray(hist)) {
          const row = hist.find((x) => x?.version === vnum)
          if (row) {
            setBackendMeta({
              editor: row?.editor || '',
              created_at: row?.created_at ?? null,
              description: row?.description || '',
              is_ai_generated: !!row?.is_ai_generated,
            })
          }
        }
      } catch {
        /* 刷新版本列表失败不影响保存结果 */
      }
    } catch (e) {
      console.error(e)
      setAiError(e?.message || '保存失败')
      setAiValidation(null)
    } finally {
      setAiLoading(false)
    }
  }, [backendTreeId, backendVersion, graphData, parsedInfo, rawJsonText, runRuleValidation])

  const handleSubmit = useCallback(() => {
    setModal({ type: 'saveDesc', value: '' })
  }, [])

  const handleNodeContextMenu = useCallback(
    (event, rfNode) => {
      const info = getNodeEditInfo(graphData, rfNode.id)
      if (!info) return
      setPaneMenu(null)
      setContextMenu({
        x: event.clientX,
        y: event.clientY,
        info,
      })
    },
    [graphData],
  )

  const handlePaneContextMenu = useCallback((event) => {
    event.preventDefault()
    setContextMenu(null)
    setEdgeMenu(null)
    setPaneMenu({
      x: event.clientX,
      y: event.clientY,
    })
  }, [])

  const handleNodeDoubleClick = useCallback(
    (_event, rfNode) => {
      const node = graphData.nodes.find((n) => n.id === rfNode.id)
      if (!node || node.type === 'gate') return
      setModal({ type: 'rename', nodeId: node.id, value: node.label })
    },
    [graphData],
  )

  const doAddChild = useCallback(
    (parentId) => {
      const existing = graphData.nodes.filter((n) =>
        typeof n.label === 'string' && n.label.startsWith('新中间事件'),
      )
      const nextIndex = existing.length + 1
      const name = `新中间事件${nextIndex}`
      applyEdit(addChildNode(graphData, parentId, name, 'intermediate'))
      setSelectedNode(null)
      setSelectedEdge(null)
    },
    [graphData, applyEdit],
  )

  const doAddChildUnderGate = useCallback(
    (gateId) => {
      const existing = graphData.nodes.filter((n) =>
        typeof n.label === 'string' && n.label.startsWith('新底事件'),
      )
      const nextIndex = existing.length + 1
      const name = `新底事件${nextIndex}`
      applyEdit(addChildUnderGate(graphData, gateId, name, 'basic'))
      setSelectedNode(null)
      setSelectedEdge(null)
    },
    [graphData, applyEdit],
  )

  const doDelete = useCallback(
    (nodeId) => {
      applyEdit(deleteNode(graphData, nodeId))
      setSelectedNode(null)
      setSelectedEdge(null)
    },
    [graphData, applyEdit],
  )

  const doDeleteGate = useCallback(
    (gateId) => {
      applyEdit(deleteGate(graphData, gateId))
      setSelectedNode(null)
    },
    [graphData, applyEdit],
  )

  const doChangeGate = useCallback(
    (gateId, newType) => {
      applyEdit(changeGateType(graphData, gateId, newType))
    },
    [graphData, applyEdit],
  )

  const doRename = useCallback(
    (nodeId, newName) => {
      applyEdit(renameNode(graphData, nodeId, newName))
      const sel = selectedNode
      if (sel && sel.id === nodeId) {
        setSelectedNode({ ...sel, data: { ...sel.data, label: newName } })
      }
    },
    [graphData, applyEdit, selectedNode],
  )

  const doChangeType = useCallback(
    (nodeId, newType) => {
      applyEdit(changeEventType(graphData, nodeId, newType))
      setSelectedNode((sel) => {
        if (!sel || sel.id !== nodeId) return sel
        return {
          ...sel,
          data: {
            ...sel.data,
            type: newType,
          },
        }
      })
    },
    [graphData, applyEdit],
  )

  const doChangeDescription = useCallback(
    (nodeId, newDesc) => {
      applyEdit(changeEventDescription(graphData, nodeId, newDesc))
    },
    [graphData, applyEdit],
  )

  const doPatchEvent = useCallback(
    (nodeId, patch) => {
      applyEdit(patchEventFields(graphData, nodeId, patch))
    },
    [graphData, applyEdit],
  )

  /** 传入该节点下全部 chunk id（用于多 chunk 时标题栏 ◀/▶），可选 preferActiveId 为当前要点开的那条 */
  const openChunkPanel = useCallback((chunkIds, preferActiveId) => {
    const ids = (Array.isArray(chunkIds) ? chunkIds : [chunkIds])
      .map((x) => {
        if (x && typeof x === 'object') {
          const chunkId = x.chunk_id ?? x.chunkId ?? x.id
          if (chunkId === undefined || chunkId === null || chunkId === '') return null
          return {
            chunk_id: chunkId,
            file_version_id: x.file_version_id ?? x.fileVersionId ?? '',
            chunk_name: x.chunk_name ?? x.chunkName ?? '',
          }
        }
        if (x === undefined || x === null || x === '') return null
        return { chunk_id: x, file_version_id: '' }
      })
      .filter(Boolean)
    if (!ids.length) return
    setChunkPanelOpen(true)
    setChunkPanelChunkIds(ids)
    const pref = makeChunkRefKey(preferActiveId)
    const next =
      pref && ids.some((item) => makeChunkRefKey(item) === pref) ? pref : makeChunkRefKey(ids[0])
    setChunkPanelActiveId(next)
  }, [])

  useEffect(() => {
    if (!chunkPanelOpen || !chunkPanelActiveId) return
    if (chunkPanelData[String(chunkPanelActiveId)]) return
    const activeRef = chunkPanelChunkIds.find((item) => makeChunkRefKey(item) === String(chunkPanelActiveId))
    if (!activeRef) return

    const controller = new AbortController()
    setChunkPanelLoading(true)
    setChunkPanelError('')
    ;(async () => {
      try {
        const data = await getFtaChunk({
          chunkId: activeRef.chunk_id,
          fileVersionId: activeRef.file_version_id,
          signal: controller.signal,
        })
        setChunkPanelData((prev) => ({ ...prev, [String(chunkPanelActiveId)]: data }))
      } catch (e) {
        if (e?.name === 'AbortError') return
        setChunkPanelError(e?.message || '加载 chunk 内容失败')
      } finally {
        setChunkPanelLoading(false)
      }
    })()

    return () => controller.abort()
  }, [chunkPanelOpen, chunkPanelActiveId, chunkPanelData, chunkPanelChunkIds])

  const doInsertGate = useCallback(
    (parentId, gateType) => {
      applyEdit(insertGate(graphData, parentId, gateType))
    },
    [graphData, applyEdit],
  )

  const doInsertParent = useCallback(
    (childId) => {
      applyEdit(insertParentEvent(graphData, childId))
    },
    [graphData, applyEdit],
  )

  const doDuplicate = useCallback(
    (nodeId) => {
      applyEdit(duplicateEventNode(graphData, nodeId))
    },
    [graphData, applyEdit],
  )

  const doAddStandaloneNode = useCallback(
    (eventType) => {
      const baseLabel =
        eventType === 'top'
          ? '新顶事件'
          : eventType === 'intermediate'
            ? '新中间事件'
            : '新底事件'
      const existing = graphData.nodes.filter(
        (n) => typeof n.label === 'string' && n.label.startsWith(baseLabel),
      )
      const nextIndex = existing.length + 1
      const name = `${baseLabel}${nextIndex}`

      const newId = `node-${Date.now().toString(36)}${Math.random()
        .toString(36)
        .slice(2, 9)}`

      const newNode = {
        id: newId,
        label: name,
        type: eventType,
        position: { x: 0, y: 0 },
        meta: {
          rawType: eventType === 'top' ? '1' : eventType === 'intermediate' ? '2' : '3',
          gateCode: '',
          gateLabel: '',
          event: {
            id: newId,
            name,
          },
          transfer: '',
        },
      }

      const nextGraph = {
        nodes: [...graphData.nodes, newNode],
        edges: [...graphData.edges],
      }
      applyEdit(nextGraph)
      setPaneMenu(null)
    },
    [graphData, applyEdit],
  )

  const handleConnect = useCallback(
    (params) => {
      const { source, target } = params || {}
      if (!source || !target) return
      // React Flow 里用户通常是从父节点拖到子节点；
      // 我们内部的图结构采用 child -> parent，因此这里反转一次。
      applyEdit(connectNodes(graphData, { sourceId: target, targetId: source }))
    },
    [graphData, applyEdit],
  )

  const handleEdgeContextMenu = useCallback((event, edge) => {
    setEdgeMenu({
      x: event.clientX,
      y: event.clientY,
      edgeId: edge.id,
    })
  }, [])

  const doDeleteEdge = useCallback(
    (edgeId) => {
      applyEdit(deleteEdgeById(graphData, edgeId))
      setEdgeMenu(null)
      setSelectedEdge(null)
    },
    [graphData, applyEdit],
  )

  const handleUndo = useCallback(() => {
    setHistory((h) => {
      if (!h.length) return h
      const last = h[h.length - 1]
      setRedoHistory((r) => [...r, { graphData, rawJsonText }])
      setGraphData(last.graphData)
      setRawJsonText(last.rawJsonText)
      setError('')
      return h.slice(0, -1)
    })
  }, [graphData, rawJsonText])

  const handleRedo = useCallback(() => {
    setRedoHistory((r) => {
      if (!r.length) return r
      const last = r[r.length - 1]
      setHistory((h) => [...h, { graphData, rawJsonText }])
      setGraphData(last.graphData)
      setRawJsonText(last.rawJsonText)
      setError('')
      return r.slice(0, -1)
    })
  }, [graphData, rawJsonText])

  const hasGraph = useMemo(
    () => graphData.nodes.length > 0,
    [graphData.nodes.length],
  )

  const hiResDisabled = hiResSnapshotText.length > MAX_HIRES_SNAPSHOT_JSON_LEN

  const selectedMeta = selectedNode?.data?.meta

  function renderContextMenu() {
    if (!contextMenu) return null
    const { x, y, info } = contextMenu
    const { node, isGate, gateChild, hasDirectChildren, isTop, parentId } = info

    // 固定菜单最大高度与 CSS 中的 max-height 保持一致，靠近点击位置，
    // 仅在接近窗口底部时进行少量上移，避免被裁剪
    const MENU_MAX_HEIGHT = 260
    const menuStyle = {
      left: Math.min(x, window.innerWidth - 220),
      top: Math.min(y, window.innerHeight - MENU_MAX_HEIGHT - 8),
    }

    if (isGate) {
      const otherType = node.label === 'AND' ? 'OR' : 'AND'
      return (
        <div className="fta-context-menu" style={menuStyle}>
          <button
            className="fta-context-menu-item"
            onClick={() => {
              doAddChildUnderGate(node.id)
              setContextMenu(null)
            }}
          >
            <span className="fta-ctx-icon">＋</span>
            在门下添加事件子节点
          </button>
          <div className="fta-context-menu-sep" />
          <button
            className="fta-context-menu-item"
            onClick={() => {
              doChangeGate(node.id, otherType)
              setContextMenu(null)
            }}
          >
            <span className="fta-ctx-icon">⇄</span>
            切换为 {otherType} 门
          </button>
          <div className="fta-context-menu-sep" />
          <button
            className="fta-context-menu-item fta-context-menu-item--danger"
            onClick={() => {
              doDeleteGate(node.id)
              setContextMenu(null)
            }}
          >
            <span className="fta-ctx-icon">✕</span>
            删除逻辑门
          </button>
        </div>
      )
    }

    return (
      <div className="fta-context-menu" style={menuStyle}>
        <button
          className="fta-context-menu-item"
          onClick={() => {
            doAddChild(node.id)
            setContextMenu(null)
          }}
        >
          <span className="fta-ctx-icon">＋</span>
          在其下添加子节点
        </button>
        {!isTop && (
          <button
            className="fta-context-menu-item"
            onClick={() => {
              doDuplicate(node.id)
              setContextMenu(null)
            }}
          >
            <span className="fta-ctx-icon">⧉</span>
            复制此节点
          </button>
        )}
        {!isTop && parentId && (
          <button
            className="fta-context-menu-item"
            onClick={() => {
              doInsertParent(node.id)
              setContextMenu(null)
            }}
          >
            <span className="fta-ctx-icon">⇡</span>
            在其上插入父节点
          </button>
        )}
        <button
          className="fta-context-menu-item"
          onClick={() => {
            setContextMenu(null)
            setModal({ type: 'rename', nodeId: node.id, value: node.label })
          }}
        >
          <span className="fta-ctx-icon">✎</span>
          修改名称
        </button>
        <div className="fta-context-menu-sep" />
        <div className="fta-context-menu-label">事件类型</div>
        {EVENT_TYPES.map((t) => (
          <button
            key={t}
            className={`fta-context-menu-item fta-context-menu-item--type${
              node.type === t ? ' fta-context-menu-item--active' : ''
            }`}
            disabled={node.type === t}
            onClick={() => {
              doChangeType(node.id, t)
              setContextMenu(null)
            }}
          >
            <span
              className={`fta-ctx-type-dot fta-ctx-type-dot--${t}`}
            />
            {TYPE_LABELS[t]}
          </button>
        ))}

        {gateChild && (
          <>
            <div className="fta-context-menu-sep" />
            <div className="fta-context-menu-label">逻辑门</div>
            <button
              className="fta-context-menu-item"
              onClick={() => {
                doChangeGate(
                  gateChild.id,
                  gateChild.label === 'AND' ? 'OR' : 'AND',
                )
                setContextMenu(null)
              }}
            >
              <span className="fta-ctx-icon">⇄</span>
              切换为 {gateChild.label === 'AND' ? 'OR' : 'AND'} 门
            </button>
          </>
        )}

        {!gateChild && hasDirectChildren && (
          <>
            <div className="fta-context-menu-sep" />
            <div className="fta-context-menu-label">插入逻辑门</div>
            <button
              className="fta-context-menu-item"
              onClick={() => {
                doInsertGate(node.id, 'AND')
                setContextMenu(null)
              }}
            >
              <span className="fta-ctx-icon">⊓</span>
              插入 AND 门
            </button>
            <button
              className="fta-context-menu-item"
              onClick={() => {
                doInsertGate(node.id, 'OR')
                setContextMenu(null)
              }}
            >
              <span className="fta-ctx-icon">⊔</span>
              插入 OR 门
            </button>
          </>
        )}

        {!isTop && (
          <>
            <div className="fta-context-menu-sep" />
            <button
              className="fta-context-menu-item fta-context-menu-item--danger"
              onClick={() => {
                doDelete(node.id)
                setContextMenu(null)
              }}
            >
              <span className="fta-ctx-icon">✕</span>
              删除节点
            </button>
          </>
        )}
      </div>
    )
  }

  function renderEdgeMenu() {
    if (!edgeMenu) return null
    const { x, y, edgeId } = edgeMenu
    const MENU_MAX_HEIGHT = 160
    const menuStyle = {
      left: Math.min(x, window.innerWidth - 200),
      top: Math.min(y, window.innerHeight - MENU_MAX_HEIGHT - 8),
    }
    return (
      <div
        className="fta-context-menu"
        style={menuStyle}
        onMouseLeave={() => setEdgeMenu(null)}
      >
        <button
          className="fta-context-menu-item fta-context-menu-item--danger"
          onClick={() => doDeleteEdge(edgeId)}
        >
          <span className="fta-ctx-icon">✕</span>
          删除连线
        </button>
      </div>
    )
  }

  function renderPaneMenu() {
    if (!paneMenu) return null
    const { x, y } = paneMenu
    const menuStyle = {
      left: Math.min(x, window.innerWidth - 200),
      top: Math.min(y, window.innerHeight - 180),
    }
    const hasTopEvent = graphData.nodes.some((n) => n.type === 'top')
    return (
      <div className="fta-context-menu" style={menuStyle}>
        <div className="fta-context-menu-label">在此处新增事件节点</div>
        {!hasTopEvent && (
          <button
            className="fta-context-menu-item"
            onClick={() => doAddStandaloneNode('top')}
          >
            <span className="fta-ctx-icon">◆</span>
            新建顶事件
          </button>
        )}
        <button
          className="fta-context-menu-item"
          onClick={() => doAddStandaloneNode('intermediate')}
        >
          <span className="fta-ctx-icon">◼</span>
          新建中间事件
        </button>
        <button
          className="fta-context-menu-item"
          onClick={() => doAddStandaloneNode('basic')}
        >
          <span className="fta-ctx-icon">●</span>
          新建底事件
        </button>
      </div>
    )
  }

  function renderModal() {
    if (!modal) return null

    if (modal.type === 'rename') {
      return (
        <RenameModal
          initialValue={modal.value}
          onConfirm={(name) => {
            doRename(modal.nodeId, name)
            setModal(null)
          }}
          onCancel={() => setModal(null)}
        />
      )
    }

    if (modal.type === 'saveDesc') {
      return (
        <SaveDescriptionModal
          initialValue={modal.value || ''}
          onConfirm={(desc) => {
            setModal(null)
            doSubmit(desc)
          }}
          onCancel={() => setModal(null)}
        />
      )
    }

    return null
  }

  canvasDraftStateRef.current = {
    projectIdFromQuery,
    canvasIdFromQuery,
    rawJsonText,
    baselineJsonText,
    graphData,
    backendTreeId,
    submittedToBackend,
    assistantMessages,
    assistantSnapshots,
    assistantTasks,
    selectedSourceFiles,
    assistantPending,
    assistantKbBaselineEpoch: kbSyncedBaselineEpochRef.current,
    assistantKbBaselineFvSig: kbSyncedBaselineFvSigRef.current,
  }

  assistantPersistRef.current = {
    messages: assistantMessages,
    snapshots: assistantSnapshots,
    tasks: assistantTasks,
    assistantPending,
    assistantKbBaselineEpoch: kbSyncedBaselineEpochRef.current,
    assistantKbBaselineFvSig: kbSyncedBaselineFvSigRef.current,
  }

  return (
    <div className={`fta-layout${theme === 'dark' ? ' fta-layout--dark' : ''}`}>
      <div className="fta-top-row">
        <button
          type="button"
          className="fta-btn ghost fta-back-home-btn"
          title="返回"
          aria-label="返回"
          onClick={() => {
            if (projectIdFromQuery) markProjectReviewed(projectIdFromQuery)
            navigate(projectIdFromQuery ? `/project/${projectIdFromQuery}` : '/')
          }}
        >
          <IconChevronLeft />
        </button>
        <header className="fta-header">
          <div className="fta-header-text">
            <div className="fta-header-title-row">
              <h1 className="fta-title">故障树可视化编辑</h1>
              <div className="fta-version-right">
                <div className="fta-version-toolbar">
                  <label htmlFor="fta-version-select" className="fta-version-label">
                    切换版本
                  </label>
                  <VersionSelect
                    id="fta-version-select"
                    value={backendVersion}
                    options={versionSelectOptions}
                    disabled={!backendTreeId || backendLoading}
                    onChange={handleVersionSelect}
                  />
                </div>
              </div>
            </div>
            <p className="fta-subtitle">
              右键节点可编辑 · 双击修改名称 · 操作自动同步 JSON
            </p>
          </div>
          <div className="fta-header-actions">
            <ThemeToggle variant="fta" />
            <button
              type="button"
              className="fta-btn ghost fta-header-icon-btn"
              onClick={handleUndo}
              disabled={history.length === 0}
              title="撤销"
              aria-label="撤销"
            >
              <IconUndo />
            </button>
            <button
              type="button"
              className="fta-btn ghost fta-header-icon-btn"
              onClick={handleRedo}
              disabled={redoHistory.length === 0}
              title="重做"
              aria-label="重做"
            >
              <IconRedo />
            </button>
            <button type="button" className="fta-btn ghost" onClick={handleDownloadJson}>
              下载 JSON
            </button>
            <button
              type="button"
              className="fta-btn ghost"
              onClick={() => setExportModalOpen(true)}
              disabled={!hasGraph}
            >
              导出图片
            </button>
            <div className="fta-ai-anchor">
              <button
                type="button"
                className="fta-btn ghost"
                onClick={handleCheck}
                disabled={
                  !hasGraph ||
                  validationLoading ||
                  (validation && validation.error_count > 0) ||
                  backendLoading
                }
                title="调用AI校验服务"
              >
                AI校验
              </button>
              {showAiPanel && (
                <AiValidationPanel
                  anchored
                  result={aiValidation}
                  embeddedValidation={aiEmbeddedValidation}
                  semanticValidation={aiSemanticValidation}
                  semanticLoading={aiSemanticLoading}
                  semanticError={aiSemanticError}
                  semanticNotice={aiSemanticNotice}
                  semanticStale={aiSemanticStale}
                  loading={aiLoading}
                  error={aiError}
                  onHide={() => setShowAiPanel(false)}
                />
              )}
            </div>
            <button
              type="button"
              className="fta-btn primary"
              disabled={
                !hasGraph ||
                validationLoading ||
                backendLoading ||
                !validation ||
                validation.error_count > 0 ||
                !hasUnsavedChanges
              }
              onClick={handleSubmit}
            >
              提交
            </button>
          </div>
        </header>
      </div>

      <main
        className={`fta-main${sidePanelOpen ? '' : ' fta-main--json-collapsed'}${
          explodedPanelOpen ? ' fta-main--exploded-open' : ''
        }`}
        style={
          sidePanelOpen
            ? {
                '--fta-side-panel-width': `${sidePanelWidth}px`,
                '--fta-side-panel-min': `${explodedPanelOpen ? 360 : 240}px`,
              }
            : undefined
        }
      >
        {sidePanelOpen ? (
          <section ref={sidePanelRef} className="fta-side fta-side--multi">
            <div
              className="fta-side-resizer"
              role="separator"
              aria-label="调整侧栏宽度"
              aria-orientation="vertical"
              tabIndex={0}
              onMouseDown={handleSidePanelResizeStart}
            />
            <div className="fta-side-panels">
              {explodedPanelOpen ? (
                <div className="fta-side-panel fta-side-panel--exploded">
                  <div className="fta-side-head">
                    <h2 className="fta-section-title" style={{ margin: 0 }}>
                      设备爆炸图
                    </h2>
                    <button
                      type="button"
                      className="fta-json-collapse"
                      title="关闭爆炸图面板"
                      aria-label="关闭爆炸图面板"
                      onClick={() => setExplodedPanelOpen(false)}
                    >
                      <IconClose />
                    </button>
                  </div>
                  <p className="fta-section-desc">支持“事件 ↔ 部件”映射与闪烁联动。</p>
                  <div className="fta-exploded-wrap">
                    <ExplodedViewer
                      customGlbUrl={exploded3dGlbObjectUrl}
                      partDetails={exploded3dPartDetails}
                      highlightNodeName={explodedHighlightNodeName}
                      highlightSync={explodedHighlightSync}
                    />
                  </div>
                </div>
              ) : null}

              {jsonPanelOpen ? (
                <div
                  className={`fta-side-panel${
                    !explodedPanelOpen ? ' fta-side-panel--json-solo' : ''
                  }`}
                >
                  <div className="fta-side-head">
                    <h2 className="fta-section-title" style={{ margin: 0 }}>
                      故障树 JSON
                    </h2>
                    <button
                      type="button"
                      className="fta-json-collapse"
                      title="关闭 JSON 面板"
                      aria-label="关闭 JSON 面板"
                      onClick={() => setJsonPanelOpen(false)}
                    >
                      <IconClose />
                    </button>
                  </div>
                  <p className="fta-section-desc">
                    画布编辑自动同步此处。也可粘贴 JSON 后点击&quot;应用到画布&quot;。
                  </p>
                  <div className="fta-json-editor">
                    <textarea
                      className="fta-json-input"
                      value={rawJsonText}
                      onChange={handleJsonChange}
                      spellCheck={false}
                    />
                    <div className="fta-json-editor-footer">
                      <button type="button" className="fta-btn primary" onClick={handleApplyJson}>
                        应用到画布
                      </button>
                    </div>
                  </div>
                  {notice && <p className="fta-notice-text">{notice}</p>}
                  {error && <p className="fta-error-text">{error}</p>}
                </div>
              ) : null}
            </div>
          </section>
        ) : null}

        <section className="fta-canvas-section">
          {!sidePanelOpen && (
            <div className="fta-side-expand-stack" aria-hidden={false}>
              <button
                type="button"
                className="fta-json-expand"
                title="展开 JSON 面板"
                aria-label="展开 JSON 面板"
                onClick={() => setJsonPanelOpen(true)}
              >
                <IconBraces />
              </button>
              <button
                type="button"
                className="fta-json-expand fta-json-expand--exploded"
                title="展开设备爆炸图"
                aria-label="展开设备爆炸图"
                onClick={() => setExplodedPanelOpen(true)}
              >
                <IconCube />
              </button>
            </div>
          )}

          {sidePanelOpen && !jsonPanelOpen && (
            <button
              type="button"
              className="fta-json-expand"
              title="展开 JSON 面板"
              aria-label="展开 JSON 面板"
              onClick={() => setJsonPanelOpen(true)}
            >
              <IconBraces />
            </button>
          )}

          {sidePanelOpen && !explodedPanelOpen && (
            <button
              type="button"
              className="fta-json-expand fta-json-expand--exploded"
              title="展开设备爆炸图"
              aria-label="展开设备爆炸图"
              onClick={() => setExplodedPanelOpen(true)}
            >
              <IconCube />
            </button>
          )}
          <div className="fta-canvas-toolbar">
            <h2 className="fta-section-title" style={{ margin: 0 }}>
              故障树画布
            </h2>
            <span className="fta-toolbar-hint">
              右键节点编辑 · 双击修改名称 · 拖拽平移 · 滚轮缩放
            </span>
            <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
              <button
                type="button"
                className={`fta-btn-xs${assistantOpen ? ' primary' : ' ghost'}`}
                onClick={() => setAssistantOpen((v) => !v)}
                title={assistantOpen ? '隐藏 AI 对话助手' : '展开 AI 对话助手'}
              >
                AI助手
              </button>
              <button
                type="button"
                className={`fta-btn-xs${viewMode === 'type' ? ' primary' : ' ghost'}`}
                onClick={() => setViewMode('type')}
              >
                普通视图
              </button>
              <button
                type="button"
                className={`fta-btn-xs${
                  viewMode === 'errorLevel' ? ' primary' : ' ghost'
                }`}
                onClick={() => setViewMode('errorLevel')}
              >
                错误等级视图
              </button>
              <button
                type="button"
                className={`fta-btn-xs${
                  viewMode === 'probability' ? ' primary' : ' ghost'
                }`}
                onClick={() => setViewMode('probability')}
              >
                概率视图
              </button>
            </div>
          </div>
          <div className="fta-canvas-stack">
            <div
              className={`fta-canvas-split${assistantOpen ? '' : ' fta-canvas-split--assistant-collapsed'}`}
              style={assistantOpen ? { '--fta-assistant-width': `${assistantPanelWidth}px` } : undefined}
            >
              <div className="fta-canvas-left">
                <div
                  ref={canvasRef}
                  className={`fta-canvas-wrapper${exporting ? ' fta-canvas-exporting' : ''}`}
                >
                  <FaultTreeCanvas
                    graphData={graphData}
                    onNodeSelect={handleNodeSelect}
                    onNodeContextMenu={handleNodeContextMenu}
                    onPaneContextMenu={handlePaneContextMenu}
                    onNodeDoubleClick={handleNodeDoubleClick}
                    onConnectEdge={handleConnect}
                    onEdgeSelect={handleEdgeSelect}
                    onEdgeContextMenu={handleEdgeContextMenu}
                    canvasActionsRef={canvasActionsRef}
                    theme={theme}
                    viewMode={viewMode}
                    legendPosition={jsonPanelOpen ? 'bottom-left' : 'top-left'}
                    showChrome
                  />
                  {(selectedNode || selectedEdge) && (
                    <div
                      className={`fta-right-docks${
                        chunkPanelOpen ? ' fta-right-docks--chunk-open' : ''
                      }`}
                    >
                      <div className="fta-right-dock fta-right-dock--node">
                        {selectedNode ? (
                          <NodeInfoPanel
                            selectedNode={selectedNode}
                            selectedMeta={selectedMeta}
                            onClose={() => setSelectedNode(null)}
                            onRename={(newName) => doRename(selectedNode.id, newName)}
                            onChangeType={(newType) => doChangeType(selectedNode.id, newType)}
                            onDelete={() => doDelete(selectedNode.id)}
                            onChangeDescription={(newDesc) =>
                              doChangeDescription(selectedNode.id, newDesc)
                            }
                            onPatchEvent={(patch) => doPatchEvent(selectedNode.id, patch)}
                            onOpenChunks={(ids, activeId) =>
                              openChunkPanel(ids, activeId)
                            }
                          />
                        ) : (
                          <EdgeInfoPanel
                            selectedEdge={selectedEdge}
                            graphData={graphData}
                            onClose={() => setSelectedEdge(null)}
                            onOpenChunks={(ids, activeId) =>
                              openChunkPanel(ids, activeId)
                            }
                          />
                        )}
                      </div>

                      {chunkPanelOpen && (
                        <div className="fta-right-dock fta-right-dock--chunk">
                          <ChunkViewerPanel
                            chunkIds={chunkPanelChunkIds}
                            activeId={chunkPanelActiveId}
                            onSelect={(id) => setChunkPanelActiveId(id)}
                            onClose={() => setChunkPanelOpen(false)}
                            loading={chunkPanelLoading}
                            error={chunkPanelError}
                            data={chunkPanelData[String(chunkPanelActiveId)] || null}
                          />
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
              {assistantOpen && (
                <>
                  <div
                    className="fta-assistant-resizer"
                    role="separator"
                    aria-label="调整 AI 助手宽度"
                    aria-orientation="vertical"
                    tabIndex={0}
                    onMouseDown={handleAssistantResizeStart}
                  />
                  <aside ref={assistantPanelRef} className="fta-canvas-right" aria-label="AI 对话助手">
                  <FaultTreeAssistantPanel
                    selectedSourceFiles={selectedSourceFiles}
                    onSelectSourceFiles={handleSelectSourceFiles}
                    fileOptions={assistantEligibleFiles}
                    messages={assistantMessages}
                    busy={assistantBusy}
                    input={assistantInput}
                    onInput={setAssistantInput}
                    onSend={handleAssistantSend}
                    pending={assistantPending}
                    onAccept={handleAssistantAccept}
                    onUndo={handleAssistantUndo}
                    onRollbackUserMessage={handleRollbackUserMessage}
                    onOpenTaskHistory={(taskId) => setAssistantProgressModal({ open: true, taskId })}
                  />
                  </aside>
                </>
              )}
            </div>
          </div>
        </section>
      </main>

      <ValidationPanel
        title="逻辑校验"
        validation={validation}
        loading={validationLoading}
        error={validationError}
        show={showValidationPanel}
        onToggle={() => setShowValidationPanel((v) => !v)}
      />

      <TaskProgressHistoryModal
        open={!!assistantProgressModal.open}
        onClose={() => setAssistantProgressModal({ open: false, taskId: '' })}
        taskId={assistantProgressModal.taskId}
        title={assistantTasks.find((t) => t.id === assistantProgressModal.taskId)?.title || ''}
        quote={assistantTasks.find((t) => t.id === assistantProgressModal.taskId)?.userPrompt || ''}
        events={assistantTaskEventHistoryRef.current.get(assistantProgressModal.taskId) || []}
      />

      {renderContextMenu()}
      {renderEdgeMenu()}
      {renderPaneMenu()}
      {renderModal()}
      {exportModalOpen && (
        <ExportImageModal
          onClose={() => setExportModalOpen(false)}
          onSimple={() => {
            setExportModalOpen(false)
            handleDownloadImage()
          }}
          onHiRes={() => {
            setExportModalOpen(false)
            handleDownloadHiResImage()
          }}
          disableHiRes={hiResDisabled}
          hiResHint={`当前故障树快照过长（修剪后长度 ${hiResSnapshotText.length} > ${MAX_HIRES_SNAPSHOT_JSON_LEN}），高保真导出可能失败。建议减少节点信息或使用“快速导出”。`}
        />
      )}
    </div>
  )
}

function TriggerRulesReadableView({ rulesText }) {
  const parsed = useMemo(() => {
    const t = (rulesText || '').trim()
    if (!t) return { ok: true, rules: [], err: '' }
    try {
      const p = JSON.parse(t)
      if (!Array.isArray(p)) return { ok: false, rules: [], err: '规则必须是 JSON 数组' }
      return { ok: true, rules: p, err: '' }
    } catch {
      return { ok: false, rules: [], err: 'JSON 格式无效' }
    }
  }, [rulesText])

  if (!parsed.ok) {
    return (
      <div className="fta-error-text" style={{ fontSize: '0.78rem' }}>
        {parsed.err}（下方可修正 JSON）
      </div>
    )
  }
  if (!parsed.rules.length) {
    return <div style={{ opacity: 0.72, fontSize: '0.82rem' }}>（空）</div>
  }
  return (
    <div className="fta-node-panel-rules fta-trigger-rules-readable">
      {parsed.rules.map((rule, idx) => (
        <p key={idx} className="fta-rule-nl-line">
          {parsed.rules.length > 1 ? (
            <span className="fta-rule-nl-idx">{idx + 1}. </span>
          ) : null}
          {formatTriggerRuleNaturalLanguage(rule)}
        </p>
      ))}
    </div>
  )
}

function EdgeInfoPanel({
  selectedEdge,
  graphData,
  onClose,
  onOpenChunks,
}) {
  const rawEdge = useMemo(() => selectedEdge?.data?.rawEdge || selectedEdge || {}, [selectedEdge])
  const relation = useMemo(
    () => selectedEdge?.data?.relation || selectedEdge?.data?.meta?.relation || rawEdge?.meta?.relation || rawEdge?.meta?.raw?.relation || {},
    [selectedEdge, rawEdge],
  )
  const semanticSource = rawEdge?.meta?.semanticSource || relation?.semanticSource || rawEdge?.source
  const semanticTarget = rawEdge?.meta?.semanticTarget || relation?.semanticTarget || rawEdge?.target
  const sourceNode = (graphData?.nodes || []).find((n) => String(n.id) === String(semanticSource))
  const targetNode = (graphData?.nodes || []).find((n) => String(n.id) === String(semanticTarget))
  const relationItems = useMemo(
    () =>
      Array.isArray(relation?.relation_bundle) && relation.relation_bundle.length
        ? relation.relation_bundle
        : [relation],
    [relation],
  )
  const evidenceTexts = relationItems.flatMap((item) => Array.isArray(item?.evidence_texts)
    ? item.evidence_texts
    : Array.isArray(item?.evidence)
      ? item.evidence.map((x) => x?.text).filter(Boolean)
      : [])
  const documents = useMemo(() => {
    const docs = relationItems.flatMap((item) => Array.isArray(item?.documents) ? item.documents : [])
    return docs
      .map((d) => {
        if (!d || typeof d !== 'object') return null
        const chunk_id = d.chunk_id ?? d.chunkId ?? d.id
        if (chunk_id === undefined || chunk_id === null || chunk_id === '') return null
        return {
          kind: 'chunk',
          chunk_id,
          chunk_name: d.chunk_name ?? d.chunkName ?? '',
          section_path: d.section_path ?? d.sectionPath ?? '',
          source_page: d.source_page ?? d.sourcePage ?? d.page ?? '',
          file_id: d.file_id ?? d.fileId ?? '',
          file_version_id: d.file_version_id ?? d.fileVersionId ?? '',
        }
      })
      .filter(Boolean)
      .filter((d, idx, arr) => arr.findIndex((x) => makeChunkRefKey(x) === makeChunkRefKey(d)) === idx)
      .sort((a, b) => Number(a.chunk_id) - Number(b.chunk_id))
  }, [relationItems])
  const chunkNavIds = useMemo(() => documents, [documents])

  return (
    <div className="fta-node-panel fta-edge-panel">
      <div className="fta-node-panel-header">
        <div style={{ flex: 1 }}>
          <div className="fta-node-panel-title">关系详情</div>
          <div className="fta-node-panel-subtitle">
            {relation?.relation_bundle?.length
              ? `多个下层事件 → ${targetNode?.label || semanticTarget}`
              : `${sourceNode?.label || relation?.source_label || semanticSource} → ${targetNode?.label || relation?.target_label || semanticTarget}`}
          </div>
        </div>
        <button
          type="button"
          className="fta-btn ghost fta-node-panel-close-btn"
          title="关闭"
          aria-label="关闭"
          onClick={onClose}
        >
          <IconClose />
        </button>
      </div>
      <div className="fta-node-panel-body">
        <InfoRow label="关系类型" value={relation?.relation_type || selectedEdge?.relation || '故障触发'} />
        <InfoRow label="极性" value={relation?.polarity || ''} />
        <InfoRow label="置信度" value={relation?.certainty || ''} />
        <InfoRow label="跨 chunk" value={String(relation?.cross_chunk || '')} />
        <InfoRow label="关系 ID" value={relation?.relation_id || ''} />

        <div className="fta-node-panel-row">
          <span className="fta-node-panel-label">证据片段</span>
          <div className="fta-node-panel-value" style={{ width: '100%' }}>
            {!evidenceTexts.length && <div style={{ opacity: 0.7 }}>（空）</div>}
            {relationItems.map((item, itemIdx) => {
              const texts = Array.isArray(item?.evidence_texts)
                ? item.evidence_texts
                : Array.isArray(item?.evidence)
                  ? item.evidence.map((x) => x?.text).filter(Boolean)
                  : []
              return texts.map((text, idx) => (
                <div key={`evidence-${itemIdx}-${idx}`} className="fta-doc-tag fta-evidence-text">
                  {item?.source_label && item?.target_label ? `${item.source_label} → ${item.target_label}：` : ''}
                  {text}
                </div>
              ))
            })}
          </div>
        </div>

        <div className="fta-node-panel-row">
          <span className="fta-node-panel-label">文档溯源</span>
          <div className="fta-node-panel-value" style={{ width: '100%' }}>
            {!documents.length && <div style={{ opacity: 0.7 }}>（空）</div>}
            {documents.map((doc, idx) => (
              <button
                type="button"
                key={`${makeChunkRefKey(doc)}-${idx}`}
                className="fta-doc-card fta-doc-card--clickable"
                onClick={() => onOpenChunks?.(chunkNavIds, doc)}
                title="点击查看原始 chunk 内容"
              >
                <div className="fta-doc-card-row">
                  <span className="fta-doc-card-label">chunk_id</span>
                  <span className="fta-doc-card-value">{doc.chunk_id || '（空）'}</span>
                </div>
                <div className="fta-doc-card-row">
                  <span className="fta-doc-card-label">chunk_name</span>
                  <span className="fta-doc-card-value">{doc.chunk_name || '（空）'}</span>
                </div>
                <div className="fta-doc-card-row">
                  <span className="fta-doc-card-label">section_path</span>
                  <span className="fta-doc-card-value">{doc.section_path || '（空）'}</span>
                </div>
                <div className="fta-doc-card-row">
                  <span className="fta-doc-card-label">source_page</span>
                  <span className="fta-doc-card-value">
                    {doc.source_page === '' ? '（空）' : String(doc.source_page)}
                  </span>
                </div>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

function NodeInfoPanel({
  selectedNode,
  selectedMeta,
  onClose,
  onRename,
  onChangeType,
  onDelete,
  onChangeDescription,
  onPatchEvent,
  onOpenChunks,
}) {
  const [editingName, setEditingName] = useState(false)
  const [nameValue, setNameValue] = useState(selectedNode.data.label)
  const isGate = selectedNode.data.type === 'gate'
  const isTop = selectedNode.data.type === 'top'
  const [descValue, setDescValue] = useState(
    selectedMeta?.event?.description || '',
  )
  const [eventId, setEventId] = useState(selectedMeta?.event?.id || '')
  const [eventName, setEventName] = useState(selectedMeta?.event?.name || '')
  const [errorLevel, setErrorLevel] = useState(selectedMeta?.event?.errorLevel || '')
  const [probability, setProbability] = useState(
    selectedMeta?.event?.probability === 0
      ? '0'
      : selectedMeta?.event?.probability
        ? String(selectedMeta?.event?.probability)
        : '',
  )
  const [investigateMethod, setInvestigateMethod] = useState(
    selectedMeta?.event?.investigateMethod || '',
  )
  const documents = useMemo(() => {
    const docs = selectedMeta?.event?.documents
    if (!Array.isArray(docs)) return []
    const normalized = docs
      .map((d) => {
        if (typeof d === 'string') return { kind: 'text', text: d }
        if (d && typeof d === 'object') {
          const chunk_id = d.chunk_id ?? d.chunkId ?? d.id
          return {
            kind: 'chunk',
            chunk_id,
            chunk_name: d.chunk_name ?? d.chunkName ?? '',
            section_path: d.section_path ?? d.sectionPath ?? '',
            source_page: d.source_page ?? d.sourcePage ?? d.page ?? '',
            file_id: d.file_id ?? d.fileId ?? '',
            file_version_id: d.file_version_id ?? d.fileVersionId ?? '',
          }
        }
        return { kind: 'text', text: String(d) }
      })
      .filter(Boolean)

    const chunks = normalized
      .filter((x) => x.kind === 'chunk' && x.chunk_id !== undefined && x.chunk_id !== null && x.chunk_id !== '')
      .sort((a, b) => Number(a.chunk_id) - Number(b.chunk_id))
    const texts = normalized.filter((x) => x.kind !== 'chunk' || x.chunk_id === undefined || x.chunk_id === null || x.chunk_id === '')
    return [...chunks, ...texts]
  }, [selectedMeta?.event?.documents])
  /** 用于 chunk 原文 dock：同一节点下全部 chunk id（去重、排序），便于多 chunk 时标题栏切换 */
  const chunkNavIds = useMemo(
    () =>
      documents
        .filter((d) => d.kind === 'chunk' && d.chunk_id !== undefined && d.chunk_id !== null && d.chunk_id !== '')
        .filter((d, idx, arr) => arr.findIndex((x) => makeChunkRefKey(x) === makeChunkRefKey(d)) === idx)
        .sort((a, b) => Number(a.chunk_id) - Number(b.chunk_id)),
    [documents],
  )
  const [rulesText, setRulesText] = useState(
    Array.isArray(selectedMeta?.event?.rules)
      ? JSON.stringify(selectedMeta.event.rules, null, 2)
      : '',
  )
  const [rulesError, setRulesError] = useState('')

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    setNameValue(selectedNode.data.label)
    setEditingName(false)
    setDescValue(selectedMeta?.event?.description || '')
    setEventId(selectedMeta?.event?.id || '')
    setEventName(selectedMeta?.event?.name || '')
    setErrorLevel(selectedMeta?.event?.errorLevel || '')
    setProbability(
      selectedMeta?.event?.probability === 0
        ? '0'
        : selectedMeta?.event?.probability
          ? String(selectedMeta?.event?.probability)
          : '',
    )
    setInvestigateMethod(selectedMeta?.event?.investigateMethod || '')
    setRulesText(
      Array.isArray(selectedMeta?.event?.rules)
        ? JSON.stringify(selectedMeta.event.rules, null, 2)
        : '',
    )
    setRulesError('')
  }, [selectedNode.id, selectedNode.data.label, selectedMeta?.event?.description, selectedMeta?.event?.rules])
  /* eslint-enable react-hooks/set-state-in-effect */

  return (
    <div className="fta-node-panel">
      <div className="fta-node-panel-header">
        <div style={{ flex: 1 }}>
          {editingName ? (
            <div className="fta-inline-edit">
              <input
                className="fta-inline-edit-input"
                value={nameValue}
                onChange={(e) => setNameValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    onRename(nameValue)
                    setEditingName(false)
                  }
                  if (e.key === 'Escape') setEditingName(false)
                }}
                autoFocus
              />
              <button
                className="fta-btn-xs primary"
                onClick={() => {
                  onRename(nameValue)
                  setEditingName(false)
                }}
              >
                ✓
              </button>
              <button
                className="fta-btn-xs ghost"
                onClick={() => setEditingName(false)}
              >
                ✕
              </button>
            </div>
          ) : (
            <div className="fta-node-panel-title">
              {selectedNode.data.label}
              {!isGate && (
                <button
                  className="fta-btn-icon"
                  title="修改名称"
                  onClick={() => setEditingName(true)}
                >
                  ✎
                </button>
              )}
            </div>
          )}
          <div className="fta-node-panel-subtitle">
            ID：{selectedNode.id}
            {selectedNode.data.type && (
              <> · 类型：{TYPE_LABELS[selectedNode.data.type] || selectedNode.data.type}</>
            )}
          </div>
        </div>
        <button
          type="button"
          className="fta-btn ghost fta-node-panel-close-btn"
          title="关闭"
          aria-label="关闭"
          onClick={onClose}
        >
          <IconClose />
        </button>
      </div>

      {!isGate && (
        <div className="fta-node-panel-type-bar">
          {EVENT_TYPES.map((t) => (
            <button
              key={t}
              className={`fta-type-pill${selectedNode.data.type === t ? ' fta-type-pill--active' : ''}`}
              onClick={() => onChangeType(t)}
              disabled={selectedNode.data.type === t}
            >
              {TYPE_LABELS[t]}
            </button>
          ))}
        </div>
      )}

      {!isGate && (
        <div className="fta-node-panel-body">
          <>
              <div className="fta-node-panel-row">
                <span className="fta-node-panel-label">事件编号</span>
                <input
                  className="fta-node-panel-value"
                  value={eventId}
                  placeholder="（空）请输入事件编号"
                  onChange={(e) => setEventId(e.target.value)}
                  onBlur={() => onPatchEvent?.({ id: eventId })}
                />
              </div>
              <div className="fta-node-panel-row">
                <span className="fta-node-panel-label">事件名称</span>
                <input
                  className="fta-node-panel-value"
                  value={eventName}
                  placeholder="（空）请输入事件名称"
                  onChange={(e) => setEventName(e.target.value)}
                  onBlur={() => onPatchEvent?.({ name: eventName })}
                />
              </div>
          </>
          <div className="fta-node-panel-row">
            <span className="fta-node-panel-label">描述</span>
            <textarea
              className="fta-node-panel-value"
              style={{ width: '100%', minHeight: '4.5em', resize: 'vertical' }}
              value={descValue}
              onChange={(e) => setDescValue(e.target.value)}
              onBlur={() => onChangeDescription(descValue)}
              placeholder="（空）请输入描述"
            />
          </div>
          <>
              <div className="fta-node-panel-row">
                <span className="fta-node-panel-label">错误等级</span>
                <input
                  className="fta-node-panel-value"
                  value={errorLevel}
                  placeholder="（空）例如：高/中/低"
                  onChange={(e) => setErrorLevel(e.target.value)}
                  onBlur={() => onPatchEvent?.({ errorLevel })}
                />
              </div>
              <div className="fta-node-panel-row">
                <span className="fta-node-panel-label">概率</span>
                <input
                  className="fta-node-panel-value"
                  value={probability}
                  placeholder="（空）例如：1e-6"
                  onChange={(e) => setProbability(e.target.value)}
                  onBlur={() => {
                    if (!onPatchEvent) return
                    const t = probability.trim()
                    if (!t) {
                      onPatchEvent({ probability: null })
                      return
                    }
                    const n = Number(t)
                    if (!Number.isFinite(n)) return
                    onPatchEvent({ probability: n })
                  }}
                />
              </div>
              <div className="fta-node-panel-row">
                <span className="fta-node-panel-label">排查方法</span>
                <textarea
                  className="fta-node-panel-value"
                  style={{ width: '100%', minHeight: '3.5em', resize: 'vertical' }}
                  value={investigateMethod}
                  placeholder="（空）请输入排查方法"
                  onChange={(e) => setInvestigateMethod(e.target.value)}
                  onBlur={() => onPatchEvent?.({ investigateMethod })}
                />
              </div>
              <div className="fta-node-panel-row">
                <span className="fta-node-panel-label">文档溯源</span>
                <div className="fta-node-panel-value" style={{ width: '100%' }}>
                  {documents.length === 0 && (
                    <div style={{ opacity: 0.7 }}>（空）</div>
                  )}
                  {documents.map((doc, idx) => {
                    if (doc.kind === 'chunk') {
                      return (
                        <button
                          type="button"
                          key={`${doc.chunk_id ?? 'chunk'}-${idx}`}
                          className="fta-doc-card fta-doc-card--clickable"
                          onClick={() =>
                            onOpenChunks?.(chunkNavIds, doc)
                          }
                          title="点击查看原始 chunk 内容"
                        >
                          <div className="fta-doc-card-row">
                            <span className="fta-doc-card-label">chunk_id</span>
                            <span className="fta-doc-card-value">{doc.chunk_id || '（空）'}</span>
                          </div>
                          <div className="fta-doc-card-row">
                            <span className="fta-doc-card-label">chunk_name</span>
                            <span className="fta-doc-card-value">{doc.chunk_name || '（空）'}</span>
                          </div>
                          <div className="fta-doc-card-row">
                            <span className="fta-doc-card-label">section_path</span>
                            <span className="fta-doc-card-value">{doc.section_path || '（空）'}</span>
                          </div>
                          <div className="fta-doc-card-row">
                            <span className="fta-doc-card-label">source_page</span>
                            <span className="fta-doc-card-value">
                              {doc.source_page === '' ? '（空）' : String(doc.source_page)}
                            </span>
                          </div>
                        </button>
                      )
                    }
                    return (
                      <div key={`doc-${idx}`} className="fta-doc-tag">
                        {doc.text || '（空）'}
                      </div>
                    )
                  })}
                </div>
              </div>
              <div className="fta-node-panel-row fta-node-panel-row--rules">
                <span className="fta-node-panel-label">触发规则</span>
                <div className="fta-node-panel-value" style={{ width: '100%' }}>
                  <TriggerRulesReadableView rulesText={rulesText} />
                  <details className="fta-rules-json-details">
                    <summary className="fta-rules-json-summary">编辑原始 JSON</summary>
                    <textarea
                      className="fta-node-panel-value fta-node-panel-json-editor"
                      style={{ width: '100%', minHeight: '5.5em', resize: 'vertical' }}
                      value={rulesText}
                      placeholder="（空）请输入 JSON 数组，例如：[]"
                      onChange={(e) => {
                        setRulesText(e.target.value)
                        setRulesError('')
                      }}
                      onBlur={() => {
                        if (!onPatchEvent) return
                        const t = (rulesText || '').trim()
                        if (!t) {
                          onPatchEvent({ rules: [] })
                          return
                        }
                        try {
                          const parsed = JSON.parse(t)
                          if (!Array.isArray(parsed)) {
                            setRulesError('规则必须是 JSON 数组')
                            return
                          }
                          onPatchEvent({ rules: parsed })
                        } catch {
                          setRulesError('规则 JSON 解析失败')
                        }
                      }}
                    />
                  </details>
                  {rulesError ? <div className="fta-error-text">{rulesError}</div> : null}
                </div>
              </div>
          </>
        </div>
      )}

      {!isGate && !isTop && (
        <button
          className="fta-btn full ghost fta-btn-delete"
          onClick={onDelete}
        >
          删除此节点
        </button>
      )}
    </div>
  )
}

function InfoRow({ label, value }) {
  // 允许展示空值，让用户知道字段存在（编辑输入在 NodeInfoPanel 内处理）
  if (value === undefined || value === null || value === '') {
    return (
      <div className="fta-node-panel-row">
        <span className="fta-node-panel-label">{label}</span>
        <span className="fta-node-panel-value" style={{ opacity: 0.6 }}>
          （空）
        </span>
      </div>
    )
  }
  return (
    <div className="fta-node-panel-row">
      <span className="fta-node-panel-label">{label}</span>
      <span className="fta-node-panel-value">{String(value)}</span>
    </div>
  )
}

function ChunkViewerPanel({ chunkIds, activeId, onSelect, onClose, loading, error, data }) {
  const ids = Array.isArray(chunkIds) ? chunkIds : []
  const keys = ids.map((item) => makeChunkRefKey(item))
  const idx = activeId != null ? keys.indexOf(String(activeId)) : -1
  const multi = ids.length > 1

  const goPrev = () => {
    if (idx > 0) onSelect(keys[idx - 1])
  }
  const goNext = () => {
    if (idx >= 0 && idx < ids.length - 1) onSelect(keys[idx + 1])
  }
  const activeRef = idx >= 0 ? ids[idx] : null
  const activeChunkId = activeRef?.chunk_id ?? activeRef?.chunkId ?? activeId

  return (
    <div className="fta-chunk-panel">
      <div className="fta-chunk-panel-header">
        <div className="fta-chunk-panel-header-main">
          {multi && (
            <button
              type="button"
              className="fta-chunk-panel-nav-btn"
              title="上一个 chunk"
              onClick={goPrev}
              disabled={idx <= 0}
            >
              ◀
            </button>
          )}
          <div className="fta-chunk-panel-title">
            <span className="fta-chunk-panel-title-text">Chunk 原文</span>
            {idx >= 0 && (
              <span className="fta-chunk-panel-title-sub">
                #{activeChunkId}
                {multi ? ` (${idx + 1}/${ids.length})` : ''}
              </span>
            )}
          </div>
          {multi && (
            <button
              type="button"
              className="fta-chunk-panel-nav-btn"
              title="下一个 chunk"
              onClick={goNext}
              disabled={idx < 0 || idx >= ids.length - 1}
            >
              ▶
            </button>
          )}
        </div>
        <button
          type="button"
          className="fta-chunk-panel-hide-btn"
          title="隐藏 chunk 原文"
          onClick={onClose}
        >
          −
        </button>
      </div>

      <div className="fta-chunk-panel-body">
        {loading && <div className="fta-chunk-panel-empty">加载中…</div>}
        {!loading && error && <div className="fta-chunk-panel-error">{error}</div>}
        {!loading && !error && !data && (
          <div className="fta-chunk-panel-empty">暂无内容。</div>
        )}
        {!loading && !error && data && (
          <>
            <div className="fta-chunk-meta">
              <div className="fta-chunk-meta-row">
                <span className="fta-chunk-meta-label">chunk_id</span>
                <span className="fta-chunk-meta-value">{String(data.chunk_id ?? data.id ?? activeChunkId)}</span>
              </div>
              <div className="fta-chunk-meta-row">
                <span className="fta-chunk-meta-label">chunk_name</span>
                <span className="fta-chunk-meta-value">{data.chunk_name || '（空）'}</span>
              </div>
              <div className="fta-chunk-meta-row">
                <span className="fta-chunk-meta-label">section_path</span>
                <span className="fta-chunk-meta-value">
                  {data.section_path || data.section || data.chapter || '（空）'}
                </span>
              </div>
              <div className="fta-chunk-meta-row">
                <span className="fta-chunk-meta-label">source_page</span>
                <span className="fta-chunk-meta-value">
                  {data.source_page ?? data.source ?? '（空）'}
                </span>
              </div>
            </div>

            <pre className="fta-chunk-content">{data.content || '（空）'}</pre>
          </>
        )}
      </div>
    </div>
  )
}

function AddChildModal({ onConfirm, onCancel }) {
  const [name, setName] = useState('')
  const [type, setType] = useState('basic')
  const inputRef = useRef(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const handleSubmit = () => {
    const trimmed = name.trim()
    if (!trimmed) return
    onConfirm(trimmed, type)
  }

  return (
    <div className="fta-modal-overlay" onClick={onCancel}>
      <div className="fta-modal" onClick={(e) => e.stopPropagation()}>
        <div className="fta-modal-title">在其下添加子节点</div>
        <label className="fta-modal-label">事件名称</label>
        <input
          ref={inputRef}
          className="fta-modal-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
          placeholder="请输入事件名称"
        />
        <label className="fta-modal-label" style={{ marginTop: '0.8rem' }}>
          事件类型
        </label>
        <div className="fta-modal-types">
          {['basic', 'intermediate'].map((t) => (
            <button
              key={t}
              className={`fta-type-pill${type === t ? ' fta-type-pill--active' : ''}`}
              onClick={() => setType(t)}
            >
              {TYPE_LABELS[t]}
            </button>
          ))}
        </div>
        <div className="fta-modal-actions">
          <button className="fta-btn ghost" onClick={onCancel}>
            取消
          </button>
          <button
            className="fta-btn primary"
            onClick={handleSubmit}
            disabled={!name.trim()}
          >
            确认添加
          </button>
        </div>
      </div>
    </div>
  )
}

function RenameModal({ initialValue, onConfirm, onCancel }) {
  const [name, setName] = useState(initialValue)
  const inputRef = useRef(null)

  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [])

  const handleSubmit = () => {
    const trimmed = name.trim()
    if (!trimmed) return
    onConfirm(trimmed)
  }

  return (
    <div className="fta-modal-overlay" onClick={onCancel}>
      <div className="fta-modal" onClick={(e) => e.stopPropagation()}>
        <div className="fta-modal-title">修改名称</div>
        <input
          ref={inputRef}
          className="fta-modal-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
        />
        <div className="fta-modal-actions">
          <button className="fta-btn ghost" onClick={onCancel}>
            取消
          </button>
          <button
            className="fta-btn primary"
            onClick={handleSubmit}
            disabled={!name.trim()}
          >
            确认
          </button>
        </div>
      </div>
    </div>
  )
}

function ExportImageModal({
  onClose,
  onSimple,
  onHiRes,
  disableHiRes = false,
  hiResHint = '',
}) {
  return (
    <div className="fta-modal-overlay" onClick={onClose}>
      <div className="fta-modal" onClick={(e) => e.stopPropagation()}>
        <div className="fta-modal-title">选择导出方式</div>
        <div className="fta-modal-body">
          <p style={{ marginBottom: '0.8rem', lineHeight: 1.5 }}>
            请选择导出图片的方式：
          </p>
          <div className="fta-modal-actions" style={{ justifyContent: 'space-around' }}>
            <button
              type="button"
              className="fta-btn ghost"
              onClick={onSimple}
            >
              快速导出
            </button>
            <button
              type="button"
              className="fta-btn primary"
              onClick={onHiRes}
              disabled={disableHiRes}
            >
              高保真导出
            </button>
          </div>
          {disableHiRes && hiResHint && (
            <div className="fta-hires-disabled-hint">{hiResHint}</div>
          )}
        </div>
        <div className="fta-modal-actions" style={{ marginTop: '0.8rem' }}>
          <button type="button" className="fta-btn ghost" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </div>
  )
}

function ValidationPanel({ title = '逻辑校验', validation, loading, error, show, onToggle }) {
  const hasIssues =
    validation && Array.isArray(validation.issues) && validation.issues.length > 0
  const hasErrors = validation && validation.error_count > 0
  const hasWarnings = validation && validation.warning_count > 0

  return (
    <div className="fta-validation-panel">
      <div className="fta-validation-header" onClick={onToggle}>
        <span className="fta-validation-title">
          {title}
          {loading && <span className="fta-validation-badge">校验中…</span>}
          {!loading && validation && hasErrors && (
            <span className="fta-validation-badge error">
              错误 {validation.error_count} · 警告 {validation.warning_count}
            </span>
          )}
          {!loading && validation && !hasErrors && hasWarnings && (
            <span className="fta-validation-badge error">
              警告 {validation.warning_count}
            </span>
          )}
          {!loading && validation && validation.passed && hasIssues && !hasWarnings && (
            <span className="fta-validation-badge info">
              通过（提示 {validation.info_count}）
            </span>
          )}
          {!loading && validation && validation.passed && !hasIssues && (
            <span className="fta-validation-badge success">通过</span>
          )}
        </span>
        <button
          type="button"
          className="fta-validation-toggle"
          onClick={(e) => {
            e.stopPropagation()
            onToggle()
          }}
        >
          {show ? '收起' : '展开'}
        </button>
      </div>

      {show && (
        <div className="fta-validation-body">
          {error && <div className="fta-validation-error">校验服务错误：{error}</div>}

          {!error && !validation && !loading && (
            <div className="fta-validation-empty">暂无校验结果。</div>
          )}

          {!error && validation && (
            <ul className="fta-validation-list">
              {validation.issues.map((iss, idx) => (
                <li
                  key={`${iss.code}-${idx}`}
                  className={`fta-validation-item fta-validation-item--${iss.level?.toLowerCase()}`}
                >
                  <div className="fta-validation-item-header">
                    <span className="fta-validation-level">{iss.level}</span>
                    <span className="fta-validation-code">{iss.code}</span>
                  </div>
                  <div className="fta-validation-message">{iss.message}</div>
                  {Array.isArray(iss.node_ids) && iss.node_ids.length > 0 && (
                    <div className="fta-validation-nodes">
                      相关节点：{iss.node_ids.join(', ')}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

function AiValidationPanel({
  anchored = false,
  result,
  embeddedValidation,
  semanticValidation,
  semanticLoading,
  semanticError,
  semanticNotice,
  semanticStale,
  loading,
  error,
  onHide,
}) {
  const hasContent =
    !!result &&
    (result.suggestions ||
      (Array.isArray(result.issues) && result.issues.length > 0) ||
      result.summary ||
      result.text)

  const hasEmbedded =
    !!embeddedValidation &&
    Array.isArray(embeddedValidation.issues) &&
    embeddedValidation.issues.length > 0

  const hasSemantic =
    !!semanticValidation &&
    Array.isArray(semanticValidation.issues) &&
    semanticValidation.issues.length > 0

  let displayText = ''
  if (result) {
    // 优先展示后端返回的完整建议文本，避免 summary 与正文重复显示
    if (typeof result.suggestions === 'string') {
      displayText = result.suggestions
    } else if (typeof result.text === 'string') {
      displayText = result.text
    } else if (typeof result.summary === 'string') {
      displayText = result.summary
    }
  }

  return (
    <div className={`fta-ai-panel${anchored ? ' fta-ai-panel--anchored' : ''}`}>
      <div className="fta-ai-header">
        <span className="fta-ai-title">AI 校验与优化建议</span>
        <div className="fta-ai-header-right">
          {loading && <span className="fta-ai-badge">生成中…</span>}
          {!loading && !error && hasContent && (
            <span className="fta-ai-badge success">已生成</span>
          )}
          {!loading && !error && !hasContent && (
            <span className="fta-ai-badge muted">待提交</span>
          )}
          <button
            type="button"
            className="fta-ai-hide-btn"
            title="隐藏"
            aria-label="隐藏"
            onClick={onHide}
          >
            <IconMinus />
          </button>
        </div>
      </div>
      <div className="fta-ai-body">
        {error && <div className="fta-ai-error">AI 校验服务错误：{error}</div>}
        {semanticError && <div className="fta-ai-error">AI 语义校验错误：{semanticError}</div>}
        {semanticNotice && <div className="fta-ai-notice">{semanticNotice}</div>}
        {!semanticError && semanticLoading && (
          <div className="fta-ai-empty">AI 语义校验中…</div>
        )}
        {!semanticError && !semanticLoading && hasSemantic && (
          <div className="fta-ai-embedded-validation fta-ai-embedded-validation--semantic">
            <div className="fta-ai-embedded-title">AI 语义校验（手动校验）</div>
            <ul className="fta-validation-list" style={{ marginTop: '0.5rem' }}>
              {semanticValidation.issues.map((iss, idx) => (
                <li
                  key={`${iss.code || 'AI'}-${idx}`}
                  className={`fta-validation-item fta-validation-item--${iss.level?.toLowerCase?.() || 'info'}`}
                >
                  <div className="fta-validation-item-header">
                    <span className="fta-validation-level">{iss.level}</span>
                    <span className="fta-validation-code">{iss.code}</span>
                  </div>
                  <div className="fta-validation-message">{iss.message}</div>
                  {iss.node_name && (
                    <div className="fta-validation-nodes">相关节点：{iss.node_name}</div>
                  )}
                </li>
              ))}
            </ul>
            {semanticStale && (
              <div className="fta-ai-stale-mask">
                <div className="fta-ai-stale-text">
                  该 AI 语义校验结果暂未更新（你已修改故障树）。请点击右上角“校验”重新生成。
                </div>
              </div>
            )}
          </div>
        )}
        {!error && hasEmbedded && (
          <div className="fta-ai-embedded-validation">
            <div className="fta-ai-embedded-title">AI 语义校验（生成时）</div>
            <ul className="fta-validation-list" style={{ marginTop: '0.5rem' }}>
              {embeddedValidation.issues.map((iss, idx) => (
                <li
                  key={`${iss.code || 'AI'}-${idx}`}
                  className={`fta-validation-item fta-validation-item--${iss.level?.toLowerCase?.() || 'info'}`}
                >
                  <div className="fta-validation-item-header">
                    <span className="fta-validation-level">{iss.level}</span>
                    <span className="fta-validation-code">{iss.code}</span>
                  </div>
                  <div className="fta-validation-message">{iss.message}</div>
                  {iss.node_name && (
                    <div className="fta-validation-nodes">相关节点：{iss.node_name}</div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        {!error && !loading && !hasContent && (
          <div className="fta-ai-empty">
            通过逻辑校验后点击“提交”，将自动进行 AI 校验并生成优化建议。
          </div>
        )}
        {!error && loading && (
          <div className="fta-ai-empty">正在根据当前故障树进行 AI 分析，请稍候…</div>
        )}
        {!error && !loading && hasContent && (
          <div className="fta-ai-content">
            {displayText && <pre className="fta-ai-text">{displayText}</pre>}
            {Array.isArray(result.issues) && result.issues.length > 0 && (
              <ul className="fta-ai-issues">
                {result.issues.map((iss, idx) => (
                  <li key={idx} className="fta-ai-issue-item">
                    {iss}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export default FaultTreePage

function FaultTreeAssistantPanel({
  selectedSourceFiles,
  onSelectSourceFiles,
  fileOptions,
  messages,
  busy,
  input,
  onInput,
  onSend,
  pending,
  onAccept,
  onUndo,
  onRollbackUserMessage,
  onOpenTaskHistory,
}) {
  const [rollbackAskId, setRollbackAskId] = useState(null)
  const [kbFilesOpen, setKbFilesOpen] = useState(false)
  const chatRef = useRef(null)
  const autoScrollRef = useRef(true)
  const scrollRafRef = useRef(0)

  useEffect(() => {
    if (busy) setRollbackAskId(null)
  }, [busy])

  const scrollChatToBottom = useCallback(() => {
    const el = chatRef.current
    if (!el) return
    if (scrollRafRef.current) cancelAnimationFrame(scrollRafRef.current)
    scrollRafRef.current = requestAnimationFrame(() => {
      try {
        el.scrollTop = el.scrollHeight
      } catch {
        // ignore
      }
    })
  }, [])

  // 初次加载/新消息：若用户未主动上翻，则保持对齐最新消息
  useEffect(() => {
    if (!autoScrollRef.current) return
    scrollChatToBottom()
  }, [scrollChatToBottom, (messages || []).length, busy])

  useEffect(() => {
    return () => {
      if (scrollRafRef.current) cancelAnimationFrame(scrollRafRef.current)
    }
  }, [])

  const options = useMemo(() => {
    const list = Array.isArray(fileOptions) ? fileOptions : []
    return list.map((f) => ({ id: String(f.id), label: f.name || String(f.id) }))
  }, [fileOptions])

  const selectedSet = useMemo(() => {
    const arr = Array.isArray(selectedSourceFiles) ? selectedSourceFiles : []
    return new Set(arr.map(String))
  }, [selectedSourceFiles])

  const toggleSourceFile = useCallback(
    (id) => {
      const sid = String(id)
      const next = new Set(selectedSet)
      if (next.has(sid)) next.delete(sid)
      else next.add(sid)
      onSelectSourceFiles?.(Array.from(next))
    },
    [selectedSet, onSelectSourceFiles],
  )

  const hasPending = !!pending
  /** 有待处理的 AI 修改且当前未在生成中，才可 Accept / Undo */
  const canUndoAccept = hasPending && !busy

  return (
    <div className="fta-assistant-panel" aria-label="AI 对话助手">
      <div className="fta-assistant-header">
        <div className="fta-assistant-title-row">
          <span className="fta-assistant-title">AI 对话助手</span>
          {busy ? (
            <span className="fta-assistant-typing" aria-label="AI 正在生成">
              <span className="dot" />
              <span className="dot" />
              <span className="dot" />
            </span>
          ) : null}
        </div>
        <div className="fta-assistant-controls">
          <div className="fta-assistant-label fta-assistant-label--kb-files">
            <button
              type="button"
              id="fta-assistant-kb-files-caption"
              className="fta-assistant-kb-toggle"
              aria-expanded={kbFilesOpen}
              aria-controls="fta-assistant-kb-files-panel"
              onClick={() => setKbFilesOpen((v) => !v)}
            >
              <span className="fta-assistant-kb-toggle-label">依据文件</span>
              <span className="fta-assistant-kb-toggle-meta">
                {options.length ? `已选 ${selectedSet.size}/${options.length}` : '无可用文件'}
              </span>
              <span className="fta-assistant-kb-chevron" aria-hidden>
                {kbFilesOpen ? '▼' : '▶'}
              </span>
            </button>
            {kbFilesOpen ? (
              <div
                id="fta-assistant-kb-files-panel"
                className="fta-assistant-file-pick"
                role="group"
                aria-labelledby="fta-assistant-kb-files-caption"
                aria-multiselectable="true"
              >
                {options.length === 0 ? (
                  <div className="fta-assistant-file-empty">（项目页暂无已选知识库来源或尚未上传文件）</div>
                ) : (
                  <ul className="fta-assistant-file-list">
                    {options.map((o) => {
                      const checked = selectedSet.has(o.id)
                      return (
                        <li key={o.id} className="fta-assistant-file-item">
                          <label
                            className={`fta-assistant-file-row${checked ? ' fta-assistant-file-row--on' : ''}`}
                            title={o.label}
                          >
                            <span className="fta-assistant-file-name">{o.label}</span>
                            <input
                              type="checkbox"
                              className="fta-assistant-file-checkbox"
                              checked={checked}
                              onChange={() => toggleSourceFile(o.id)}
                            />
                          </label>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </div>
            ) : null}
          </div>
        </div>
      </div>

      <div className="fta-assistant-body">
        <div
          ref={chatRef}
          className="fta-assistant-chat"
          tabIndex={0}
          role="log"
          aria-label="对话记录（可上下滚动）"
          onScroll={(e) => {
            const el = e.currentTarget
            const dist = el.scrollHeight - el.scrollTop - el.clientHeight
            // 用户离开底部一定距离，则停止自动滚动；回到底部则恢复
            autoScrollRef.current = dist < 36
          }}
        >
          {(messages || []).map((m) => {
            const isUser = m.role === 'user'
            const isProgress = m.kind === 'progress'
            const hasRollbackTarget = isUser && m.rollbackSnapshotId
            return (
              <div
                key={m.id}
                className={`fta-assistant-msg${isUser ? ' is-user' : ' is-assistant'}`}
              >
                {isProgress ? (
                  <div className="fta-assistant-progress-card">
                    <div className="fta-assistant-progress-head">
                      <span className="fta-assistant-progress-avatar" aria-hidden>
                        {m.avatar || 'S'}
                      </span>
                      <span className="fta-assistant-progress-agent">{m.agent || '调度器'}</span>
                      {m.taskId ? (
                        <span className="fta-assistant-progress-task" title={m.taskId}>
                          #{String(m.taskId).slice(-6)}
                        </span>
                      ) : null}
                    </div>
                    {m.quote ? <div className="fta-assistant-progress-quote">引用：{m.quote}</div> : null}
                    <div className="fta-assistant-progress-body">
                      <span className="fta-assistant-progress-text">{String(m.content || '')}</span>
                      <button
                        type="button"
                        className="fta-assistant-progress-more-btn"
                        onClick={(e) => {
                          e.stopPropagation()
                          onOpenTaskHistory?.(m.taskId || '')
                        }}
                        title="点击查看该任务的历史进度与泳道图"
                      >
                        进度详情
                      </button>
                    </div>
                    {Array.isArray(m.actions) && m.actions.length > 0 ? (
                      <div className="fta-assistant-progress-actions" role="group" aria-label="选择顶事件候选">
                        {m.actions.map((a, idx) => (
                          <button
                            key={`${m.id}-act-${idx}`}
                            type="button"
                            className="fta-btn-xs ghost"
                            title={a.label}
                            disabled={Boolean(m.actionsDisabled)}
                            onClick={async (e) => {
                              e.stopPropagation()
                              const taskId = m.taskId || ''
                              if (!taskId) return
                              const pending =
                                window.__fta_pendingTopEventConfirmRef?.current?.get?.(taskId) ||
                                window.__fta_pendingTopEventConfirmRef?.get?.(taskId)
                              if (!pending?.continueWithCandidate) return
                              // 不删除此卡片；仅更新文案并禁用按钮，避免重复点击
                              window.__fta_upsertProgressMessage?.({
                                taskId,
                                agent: m.agent || '调度器',
                                avatar: m.avatar || 'S',
                                quote: m.quote || '',
                                content: `已选择候选：${a.label}（正在提交…）`,
                                actions: m.actions,
                                actionsDisabled: true,
                              })
                              try {
                                await pending.continueWithCandidate(a.candidate)
                              } catch (err) {
                                const msg = err?.message || String(err)
                                window.__fta_upsertProgressMessage?.({
                                  taskId,
                                  agent: '流程控制',
                                  avatar: 'P',
                                  quote: m.quote || '',
                                  content: `错误：${msg}`,
                                  actions: m.actions,
                                  actionsDisabled: false,
                                })
                              } finally {
                                try {
                                  window.__fta_pendingTopEventConfirmRef?.current?.delete?.(taskId)
                                } catch {
                                  // ignore
                                }
                              }
                            }}
                          >
                            选 {idx + 1}：{a.label}
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ) : isUser ? (
                  <div className="fta-assistant-user-bubble">
                    <div className="fta-assistant-msg-inner">
                      <span className="fta-assistant-msg-text">{String(m.content || '')}</span>
                    </div>
                    {hasRollbackTarget ? (
                      <button
                        type="button"
                        className="fta-assistant-rollback-arrow"
                        title={
                          busy
                            ? 'AI 正在生成，请稍后再回溯'
                            : '回溯到此消息之前的状态'
                        }
                        aria-label={busy ? '回溯对话（生成中不可用）' : '回溯对话'}
                        aria-disabled={busy}
                        onClick={() => {
                          if (busy) return
                          setRollbackAskId(m.id)
                        }}
                        disabled={busy}
                      >
                        <IconUndo />
                      </button>
                    ) : null}
                  </div>
                ) : (
                  <div className="fta-assistant-msg-btn fta-assistant-msg-btn--static">
                    <div className="fta-assistant-msg-inner">
                      {m.kind === 'thinking' ? (
                        <span className="fta-assistant-inline-typing" aria-hidden>
                          <span className="dot" />
                          <span className="dot" />
                          <span className="dot" />
                        </span>
                      ) : null}
                      <span className="fta-assistant-msg-text">
                        <AssistantMarkdownMessage content={m.content} />
                      </span>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {rollbackAskId ? (
        <div
          className="fta-assistant-dialog-overlay"
          role="presentation"
          onClick={() => setRollbackAskId(null)}
        >
          <div
            className="fta-assistant-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="确认回溯"
            onClick={(e) => e.stopPropagation()}
          >
            <p className="fta-assistant-dialog-text">确定要回溯到该条消息之前吗？之后的聊天记录将被清除，原文将填入输入框。</p>
            <div className="fta-assistant-dialog-actions">
              <button type="button" className="fta-btn-xs ghost" onClick={() => setRollbackAskId(null)}>
                取消
              </button>
              <button
                type="button"
                className="fta-btn-xs primary"
                onClick={() => {
                  onRollbackUserMessage?.(rollbackAskId)
                  setRollbackAskId(null)
                }}
              >
                确定
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <div className="fta-assistant-footer">
        <div
          className={`fta-assistant-footer-actions${canUndoAccept ? ' fta-assistant-footer-actions--active' : ''}`}
        >
          <button
            type="button"
            className={`fta-btn-xs ghost fta-assistant-pair-btn${canUndoAccept ? ' fta-assistant-pair-btn--on' : ''}`}
            onClick={onUndo}
            disabled={!canUndoAccept}
          >
            Undo
          </button>
          <button
            type="button"
            className={`fta-btn-xs primary fta-assistant-pair-btn${canUndoAccept ? ' fta-assistant-pair-btn--on' : ''}`}
            onClick={onAccept}
            disabled={!canUndoAccept}
          >
            Accept
          </button>
        </div>
        <input
          className="fta-assistant-input"
          value={input}
          onChange={(e) => onInput?.(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onSend?.()
          }}
          placeholder="输入自然语言修改要求，例如：新增一个底事件；重命名选中节点为“xxx”…"
        />
        <button type="button" className="fta-btn primary" onClick={onSend} disabled={!String(input || '').trim() || busy}>
          发送
        </button>
      </div>
    </div>
  )
}
