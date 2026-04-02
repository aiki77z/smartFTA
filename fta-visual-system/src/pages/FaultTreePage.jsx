import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { markProjectReviewed } from '../utils/projectStore.js'
import FaultTreeCanvas from '../components/fta/FaultTreeCanvas.jsx'
import rawFtaSample from '../raw-FTA/raw-FTA-new.json'
import {
  getTree,
  saveTree,
  validateFaultTreeGraph,
  validateTreeSemantic,
  getChunk,
} from '../api/ftaBackend.js'
import { parseRawFtaJson, parseTreeDataJson } from '../utils/ftaParser.js'
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

const TYPE_LABELS = { top: '顶事件', intermediate: '中间事件', basic: '基本事件' }
const EVENT_TYPES = ['top', 'intermediate', 'basic']
// 当快照 JSON 文本过长时，/fta-viewer?snapshot=... 可能触发 Vite 431（Request Header Fields Too Large）
// 这里先用“文本长度”做前置限制，避免高保真导出直接失败。
const MAX_HIRES_SNAPSHOT_JSON_LEN = 7000

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

function FaultTreePage() {
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
  const initialState = useMemo(() => {
    if (typeof window === 'undefined') {
      return {
        raw: JSON.stringify(rawFtaSample, null, 2),
        graph: normalizeToGraph(rawFtaSample),
      }
    }
    try {
      const params = new URLSearchParams(window.location.search)
      const snap = params.get('snapshot')
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
  const [theme, setTheme] = useState('light')
  const [exportModalOpen, setExportModalOpen] = useState(false)
  const canvasRef = useRef(null)
  const canvasActionsRef = useRef(null)

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
    // 先刷新一次规则校验
    const v = await runRuleValidation()
    if (!v || v.error_count > 0) return
    // 若没有任何新修改，则提示用户无需重复校验
    if (aiSemanticLastSig && editSignature && aiSemanticLastSig === editSignature) {
      setShowAiPanel(true)
      setAiSemanticNotice('没有新的修改。')
      return
    }
    // 规则通过且有新修改后自动进行 AI 语义校验，并在右下角显示
    setShowAiPanel(true)
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
        setBackendTreeId(treeIdFromQuery)
        setBackendVersion(ver?.version ?? null)
        // 剪下生成时附带的 validation，避免左侧 JSON 面板过长，并用于左右下角面板展示
        const { trimmed, logic, ai } = splitEmbeddedValidation(ver)
        const loadedText = JSON.stringify(trimmed, null, 2)
        setRawJsonText(loadedText)
        setBaselineJsonText(canonicalJsonString(loadedText))
        setGraphData(normalizeToGraph(trimmed))
        if (logic) setValidation(logic)
        if (ai) setAiEmbeddedValidation(ai)
      } catch (e) {
        // React Dev StrictMode 下 effect 可能被执行两次，第一次会在 cleanup 里 abort，
        // 这类 AbortError 不应展示为“真实错误”。
        if (e?.name === 'AbortError') return
        const msg = String(e?.message || '')
        if (msg.toLowerCase().includes('aborted')) return
        setError(e?.message || '从后端加载故障树失败')
      } finally {
        setBackendLoading(false)
      }
    })()

    return () => controller.abort()
  }, [treeIdFromQuery])

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
      const origin = window.location.origin
      // 高保真导出使用“修剪后的 snapshot”，避免把 description/message 等无关字段塞进 URL 导致 431
      const snapParam = encodeURIComponent(hiResSnapshotText)
      const url = `${origin}/fta-viewer?snapshot=${snapParam}`
      const resp = await fetch('http://localhost:8000/export-fault-tree-image', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url,
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

  const handleSubmit = useCallback(async () => {
    setError('')
    setNotice('')
    // 提交前强制刷新一次规则校验结果（validator-service）
    const v = await runRuleValidation()
    if (!v || v.error_count > 0) return

    if (!backendTreeId) {
      setNotice('校验通过。当前为本地演示数据（未关联后端 tree_id），无法保存到后端。')
      return
    }

    setAiLoading(true)
    setAiError('')
    setAiValidation(null)
    try {
      const treeData = extractTreeDataForBackend({ rawJsonText, graphData, parsedInfo })
      const resp = await saveTree({ treeId: backendTreeId, treeData, editor: '专家', description: '前端保存' })
      setBackendVersion(resp?.version ?? backendVersion)
      setAiValidation({
        suggestions: `已保存为版本 ${resp?.version ?? ''}。学习条目数：${resp?.learned_count ?? 0}`,
      })
      setNotice(`已保存到后端（tree_id=${backendTreeId}，version=${resp?.version ?? ''}）`)
      setBaselineJsonText(canonicalJsonString(rawJsonText))
    } catch (e) {
      console.error(e)
      setAiError(e?.message || '保存失败')
      setAiValidation(null)
    } finally {
      setAiLoading(false)
    }
  }, [backendTreeId, backendVersion, graphData, parsedInfo, rawJsonText, runRuleValidation])

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
    },
    [graphData, applyEdit],
  )

  const doAddChildUnderGate = useCallback(
    (gateId) => {
      const existing = graphData.nodes.filter((n) =>
        typeof n.label === 'string' && n.label.startsWith('新基本事件'),
      )
      const nextIndex = existing.length + 1
      const name = `新基本事件${nextIndex}`
      applyEdit(addChildUnderGate(graphData, gateId, name, 'basic'))
      setSelectedNode(null)
    },
    [graphData, applyEdit],
  )

  const doDelete = useCallback(
    (nodeId) => {
      applyEdit(deleteNode(graphData, nodeId))
      setSelectedNode(null)
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
      .map((x) => Number(x))
      .filter((x) => Number.isFinite(x))
    if (!ids.length) return
    setChunkPanelOpen(true)
    setChunkPanelChunkIds(ids)
    const pref = Number(preferActiveId)
    const next =
      Number.isFinite(pref) && ids.includes(pref) ? pref : ids[0]
    setChunkPanelActiveId(next)
  }, [])

  useEffect(() => {
    if (!chunkPanelOpen || !chunkPanelActiveId) return
    if (chunkPanelData[String(chunkPanelActiveId)]) return

    const controller = new AbortController()
    setChunkPanelLoading(true)
    setChunkPanelError('')
    ;(async () => {
      try {
        const data = await getChunk({ chunkId: chunkPanelActiveId, signal: controller.signal })
        setChunkPanelData((prev) => ({ ...prev, [String(chunkPanelActiveId)]: data }))
      } catch (e) {
        if (e?.name === 'AbortError') return
        setChunkPanelError(e?.message || '加载 chunk 内容失败')
      } finally {
        setChunkPanelLoading(false)
      }
    })()

    return () => controller.abort()
  }, [chunkPanelOpen, chunkPanelActiveId, chunkPanelData])

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
            : '新基本事件'
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
      <div className="fta-context-menu" style={menuStyle}>
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
    return (
      <div className="fta-context-menu" style={menuStyle}>
        <div className="fta-context-menu-label">在此处新增事件节点</div>
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
          新建基本事件
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

    return null
  }

  return (
    <div className={`fta-layout${theme === 'dark' ? ' fta-layout--dark' : ''}`}>
      <div className="fta-top-row">
        <button
          type="button"
          className="fta-btn ghost fta-back-home-btn"
          onClick={() => {
            if (projectIdFromQuery) markProjectReviewed(projectIdFromQuery)
            navigate(projectIdFromQuery ? `/project/${projectIdFromQuery}` : '/')
          }}
        >
          返回
        </button>
        <header className="fta-header">
          <div>
            <h1 className="fta-title">故障树可视化编辑</h1>
            <p className="fta-subtitle">
              右键节点可编辑 · 双击修改名称 · 操作自动同步 JSON
            </p>
            {backendTreeId && (
              <p className="fta-subtitle" style={{ marginTop: '0.25rem' }}>
                后端树：<strong>{backendTreeId}</strong>
                {backendVersion ? ` · 当前版本 ${backendVersion}` : ''}
                {backendLoading ? ' · 加载中…' : ''}
              </p>
            )}
          </div>
          <div className="fta-header-actions">
            <button
              type="button"
              className="fta-btn ghost"
              onClick={() =>
                setTheme((t) => (t === 'light' ? 'dark' : 'light'))
              }
            >
              {theme === 'light' ? '夜间' : '日间'}
            </button>
            <button
              type="button"
              className="fta-btn ghost"
              onClick={handleUndo}
              disabled={history.length === 0}
            >
              撤销
            </button>
            <button
              type="button"
              className="fta-btn ghost"
              onClick={handleRedo}
              disabled={redoHistory.length === 0}
            >
              重做
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
              title="调用规则校验服务 /validate-fault-tree"
            >
              校验
            </button>
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

      <main className="fta-main">
        <section className="fta-side">
          <h2 className="fta-section-title">故障树 JSON</h2>
          <p className="fta-section-desc">
            画布编辑自动同步此处。也可粘贴 JSON 后点击"应用到画布"。
          </p>
          <textarea
            className="fta-json-input"
            value={rawJsonText}
            onChange={handleJsonChange}
            spellCheck={false}
          />
          <button type="button" className="fta-btn full" onClick={handleApplyJson}>
            应用到画布
          </button>
          {notice && <p className="fta-notice-text">{notice}</p>}
          {error && <p className="fta-error-text">{error}</p>}
        </section>

        <section className="fta-canvas-section">
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
              ref={canvasRef}
              className={`fta-canvas-wrapper${exporting ? ' fta-canvas-exporting' : ''}`}
            >
              <FaultTreeCanvas
                graphData={graphData}
                onNodeSelect={setSelectedNode}
                onNodeContextMenu={handleNodeContextMenu}
                onPaneContextMenu={handlePaneContextMenu}
                onNodeDoubleClick={handleNodeDoubleClick}
                onConnectEdge={handleConnect}
                onEdgeContextMenu={handleEdgeContextMenu}
                canvasActionsRef={canvasActionsRef}
                theme={theme}
                viewMode={viewMode}
                showChrome
              />
            </div>
            {selectedNode && (
              <div
                className={`fta-right-docks${
                  chunkPanelOpen ? ' fta-right-docks--chunk-open' : ''
                }`}
              >
                <div className="fta-right-dock fta-right-dock--node">
                  <button
                    type="button"
                    className="fta-dock-arrow"
                    title="关闭节点详情"
                    onClick={() => setSelectedNode(null)}
                  >
                    ◀
                  </button>
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
        </section>
      </main>

      <ValidationPanel
        title="逻辑校验（规则引擎）"
        validation={validation}
        loading={validationLoading}
        error={validationError}
        show={showValidationPanel}
        onToggle={() => setShowValidationPanel((v) => !v)}
      />

      {showAiPanel ? (
        <AiValidationPanel
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
      ) : (
        <button
          type="button"
          className="fta-ai-minimized"
          onClick={() => setShowAiPanel(true)}
        >
          AI 建议
        </button>
      )}

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
            chunk_id: chunk_id === undefined ? undefined : Number(chunk_id),
            chunk_name: d.chunk_name ?? d.chunkName ?? '',
            section_path: d.section_path ?? d.sectionPath ?? '',
            source_page: d.source_page ?? d.sourcePage ?? d.page ?? '',
          }
        }
        return { kind: 'text', text: String(d) }
      })
      .filter(Boolean)

    const chunks = normalized
      .filter((x) => x.kind === 'chunk' && Number.isFinite(x.chunk_id))
      .sort((a, b) => a.chunk_id - b.chunk_id)
    const texts = normalized.filter((x) => x.kind !== 'chunk' || !Number.isFinite(x.chunk_id))
    return [...chunks, ...texts]
  }, [selectedMeta?.event?.documents])
  /** 用于 chunk 原文 dock：同一节点下全部 chunk id（去重、排序），便于多 chunk 时标题栏切换 */
  const chunkNavIds = useMemo(
    () =>
      [
        ...new Set(
          documents
            .filter((d) => d.kind === 'chunk' && Number.isFinite(d.chunk_id))
            .map((d) => d.chunk_id),
        ),
      ].sort((a, b) => a - b),
    [documents],
  )
  const [rulesText, setRulesText] = useState(
    Array.isArray(selectedMeta?.event?.rules)
      ? JSON.stringify(selectedMeta.event.rules, null, 2)
      : '',
  )
  const [rulesError, setRulesError] = useState('')

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
  }, [selectedNode.id, selectedNode.data.label, selectedMeta?.event?.description])

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
        <button type="button" className="fta-btn ghost" onClick={onClose}>
          关闭
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
          {!isTop && (
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
          )}
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
          {!isTop && (
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
                            onOpenChunks?.(chunkNavIds, doc.chunk_id)
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
              <div className="fta-node-panel-row">
                <span className="fta-node-panel-label">触发规则</span>
                <textarea
                  className="fta-node-panel-value"
                  style={{ width: '100%', minHeight: '6em', resize: 'vertical', fontFamily: 'monospace' }}
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
                    } catch (e) {
                      setRulesError('规则 JSON 解析失败')
                    }
                  }}
                />
              </div>
              {rulesError && <div className="fta-error-text">{rulesError}</div>}
            </>
          )}
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
  const idx = activeId != null ? ids.indexOf(activeId) : -1
  const multi = ids.length > 1

  const goPrev = () => {
    if (idx > 0) onSelect(ids[idx - 1])
  }
  const goNext = () => {
    if (idx >= 0 && idx < ids.length - 1) onSelect(ids[idx + 1])
  }

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
                #{activeId}
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
                <span className="fta-chunk-meta-value">{String(data.id ?? activeId)}</span>
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
    <div className="fta-ai-panel">
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
            onClick={onHide}
          >
            隐藏
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
