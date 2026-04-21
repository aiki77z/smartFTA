import { useEffect, useMemo, useRef, useState } from 'react'
import './swimlane-activity-diagram.css'

function textOf(e) { return String(e?.text || '') }

function findFirstSeq(events, pred) {
  for (const e of events || []) if (pred(e)) return Number(e.seq) || 0
  return null
}
function findLastSeq(events, pred) {
  let r = null
  for (const e of events || []) if (pred(e)) r = Number(e.seq) || 0
  return r
}

function normAgent(a) {
  const s = String(a || '').trim()
  if (!s) return 'Agent'
  if (s.includes('召回')) return '召回智能体'
  if (s === 'LLM#1' || s.includes('知识抽取')) return '知识抽取智能体'
  if (s === 'LLM#2' || s.includes('草稿生成')) return '草稿生成智能体'
  if (s.includes('校验')) return '结构校验智能体'
  if (s.includes('修复')) return '修复智能体'
  if (s.includes('流程')) return '流程控制'
  if (s.includes('调度')) return '调度器'
  return s
}

const C = { colW: 84, rowH: 50, nodeW: 72, nodeH: 28, dR: 22, tW: 58, tH: 24, pad: 3 }
const CC = { colW: 80, rowH: 46, nodeW: 68, nodeH: 26, dR: 20, tW: 54, tH: 22, pad: 3 }

function bounds(kind, cx, cy, c) {
  if (kind === 'decision') {
    const r = c.dR
    return { cx, cy, w: r * 2, h: r * 2, top: { x: cx, y: cy - r }, bot: { x: cx, y: cy + r }, left: { x: cx - r, y: cy }, right: { x: cx + r, y: cy } }
  }
  const w = kind === 'terminator' ? c.tW : c.nodeW
  const h = kind === 'terminator' ? c.tH : c.nodeH
  return { cx, cy, w, h, top: { x: cx, y: cy - h / 2 }, bot: { x: cx, y: cy + h / 2 }, left: { x: cx - w / 2, y: cy }, right: { x: cx + w / 2, y: cy } }
}

export default function SwimlaneActivityDiagram({ events = [], compact = false, title = '多智能体生成流程' }) {
  const baseC = compact ? CC : C
  const wrapRef = useRef(null)
  const [wrapWidth, setWrapWidth] = useState(0)

  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const el = wrapRef.current
    if (!el) return undefined
    const ro = new ResizeObserver((entries) => {
      const w = Math.round(entries?.[0]?.contentRect?.width || 0)
      if (Number.isFinite(w) && w > 0) setWrapWidth(w)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const normEv = useMemo(() =>
    (Array.isArray(events) ? events : [])
      .map(e => ({ ...e, agentNorm: normAgent(e.agent), seq: Number(e.seq) || 0, level: String(e.level || '').toUpperCase(), text: String(e.text || ''), stage: String(e.stage || '') }))
      .sort((a, b) => a.seq - b.seq),
    [events])

  const lanes = useMemo(() => [
    { id: 'scheduler', name: '调度器' },
    { id: 'recall', name: '召回' },
    { id: 'llm2', name: '草稿生成' },
    { id: 'validate', name: '结构校验' },
    { id: 'repair', name: '修复' },
    { id: 'persist', name: '持久化' },
  ], [])

  const laneIdx = useMemo(() => { const m = new Map(); lanes.forEach((l, i) => m.set(l.id, i)); return m }, [lanes])

  // 让仅剩的 6 列尽量铺满容器宽度（最小不小于默认 colW）
  const c = useMemo(() => {
    const minColW = baseC.colW
    const usable = Math.max(0, Number(wrapWidth) || 0)
    const dynamicColW = lanes.length > 0 ? Math.floor(usable / lanes.length) : minColW
    return {
      ...baseC,
      colW: Math.max(minColW, dynamicColW),
    }
  }, [baseC, wrapWidth, lanes.length])

  const graph = useMemo(() => {
    const nodes = [
      { id: 'start', lane: 'scheduler', row: 0, kind: 'terminator', label: '开始',
        match: e => textOf(e).includes('任务开始执行') },
      { id: 'graph_match', lane: 'recall', row: 1, kind: 'action', label: '图谱匹配顶事件',
        match: e => e.stage === 'graph_match' || textOf(e).includes('[graph-match]') || /matching top event/i.test(textOf(e)) },
      { id: 'graph_subgraph', lane: 'recall', row: 2, kind: 'action', label: '扩展局部子图',
        match: e => e.stage === 'graph_subgraph' || textOf(e).includes('[graph-subgraph]') || /expanding local graph/i.test(textOf(e)) },
      { id: 'graph_chunks', lane: 'recall', row: 3, kind: 'action', label: '召回证据 Chunk',
        match: e => e.stage === 'graph_chunks' || /collecting subgraph evidence/i.test(textOf(e)) },
      { id: 'draft', lane: 'llm2', row: 4, kind: 'action', label: '生成草稿',
        match: e =>
          e.stage === 'draft' ||
          e.stage === 'generate_draft' ||
          textOf(e).includes('生成草稿') ||
          textOf(e).includes('LLM#2') ||
          textOf(e).includes('[graph-llm]') ||
          textOf(e).includes('[graph-draft]') },
      { id: 'chk_valid', lane: 'validate', row: 5, kind: 'decision', label: '校验通过？',
        match: e =>
          textOf(e).includes('草稿结构校验通过') ||
          textOf(e).includes('草稿校验失败') ||
          textOf(e).includes('[graph-validate]') },
      { id: 'retry', lane: 'llm2', row: 6, kind: 'action', label: '重新生成',
        match: e => textOf(e).includes('LLM#2：生成草稿（第 2 次）') || textOf(e).includes('[graph-regenerate]') },
      { id: 'valid_ok', lane: 'validate', row: 7, kind: 'action', label: '校验通过',
        match: e => textOf(e).includes('草稿结构校验通过') || /validation passed/i.test(textOf(e)) },
      { id: 'repair', lane: 'repair', row: 8, kind: 'action', label: '历史修正',
        match: e =>
          e.stage === 'repair' ||
          e.stage === 'fix' ||
          String(e.agent || '').includes('修复') ||
          textOf(e).includes('[修复]') ||
          textOf(e).includes('[history-repair]') },
      { id: 'persist', lane: 'persist', row: 9, kind: 'action', label: '保存版本',
        match: e => e.stage === 'persistence' || textOf(e).includes('persisting') },
      { id: 'chk_save', lane: 'persist', row: 10, kind: 'decision', label: '成功？',
        match: e => e.stage === 'completed' || e.stage === 'failed' || textOf(e).includes('生成完成') || textOf(e).includes('生成失败') },
      { id: 'done', lane: 'persist', row: 11, kind: 'terminator', label: '完成',
        match: e => textOf(e).includes('生成完成') || e.stage === 'completed' },
      { id: 'failed', lane: 'repair', row: 11, kind: 'terminator', label: '失败',
        match: e => e.stage === 'failed' || (String(e.level || '').toUpperCase() === 'ERROR' && textOf(e).includes('生成失败')) },
    ]
    const edges = [
      { from: 'start', to: 'graph_match' },
      { from: 'graph_match', to: 'graph_subgraph' },
      { from: 'graph_subgraph', to: 'graph_chunks' },
      { from: 'graph_chunks', to: 'draft' },
      { from: 'draft', to: 'chk_valid' },
      { from: 'chk_valid', to: 'valid_ok', label: '是' },
      { from: 'chk_valid', to: 'retry', label: '否' },
      { from: 'retry', to: 'chk_valid' },
      { from: 'valid_ok', to: 'repair' },
      { from: 'repair', to: 'persist' },
      { from: 'persist', to: 'chk_save' },
      { from: 'chk_save', to: 'done', label: '是' },
      { from: 'chk_save', to: 'failed', label: '否' },
    ]
    return { nodes, edges }
  }, [])

  const maxRow = 11

  const pos = useMemo(() => {
    const m = new Map()
    for (const n of graph.nodes) {
      const col = laneIdx.get(n.lane) ?? 0
      const cx = col * c.colW + c.colW / 2
      const cy = n.row * c.rowH + c.rowH / 2
      m.set(n.id, { col, row: n.row, ...bounds(n.kind, cx, cy, c) })
    }
    return m
  }, [graph.nodes, laneIdx, c])

  const nodeMap = useMemo(() => { const m = new Map(); graph.nodes.forEach(n => m.set(n.id, n)); return m }, [graph.nodes])

  const status = useMemo(() => {
    const ni = new Map()
    for (const n of graph.nodes) {
      const f = findFirstSeq(normEv, n.match), l = findLastSeq(normEv, n.match)
      ni.set(n.id, { seen: f != null, firstSeq: f, lastSeq: l })
    }
    const failedSeq = findFirstSeq(normEv, e => e.stage === 'failed' || e.level === 'ERROR')
    const doneSeq = findFirstSeq(normEv, e => e.stage === 'completed' || textOf(e).includes('生成完成'))
    /**
     * 当前高亮步骤：按流水线从后往前取「最后一个已匹配到事件」的节点。
     * 若用 max(lastSeq) 跨节点比较，draft 曾匹配 stage=graph_llm 的进度事件，seq 可能被抬得比校验/持久化还高，导致一直卡在「生成草稿」。
     */
    const PIPELINE_ORDER = [
      'start',
      'graph_match',
      'graph_subgraph',
      'graph_chunks',
      'draft',
      'chk_valid',
      'retry',
      'valid_ok',
      'repair',
      'persist',
      'chk_save',
      'done',
      'failed',
    ]
    let curId = null
    if (failedSeq != null && ni.get('failed')?.seen) {
      curId = 'failed'
    } else {
      for (let i = PIPELINE_ORDER.length - 1; i >= 0; i -= 1) {
        const id = PIPELINE_ORDER[i]
        if (ni.get(id)?.seen) {
          curId = id
          break
        }
      }
    }
    return { ni, failedSeq, doneSeq, curId }
  }, [graph.nodes, normEv])

  const isDone = id => !!status.ni.get(id)?.seen && (status.failedSeq == null || (status.ni.get(id)?.firstSeq ?? 0) <= status.failedSeq)
  const isCur = id => id === status.curId && !status.doneSeq && !status.failedSeq
  const isFail = id => id === 'failed' && status.failedSeq != null
  const isOk = id => id === 'done' && status.doneSeq != null

  const svgW = lanes.length * c.colW
  const svgH = (maxRow + 1) * c.rowH

  const edgePaths = useMemo(() => {
    const p = c.pad
    return graph.edges.map((e, i) => {
      const s = pos.get(e.from), t = pos.get(e.to)
      if (!s || !t) return null
      const sn = nodeMap.get(e.from)
      const done = !!status.ni.get(e.from)?.seen && !!status.ni.get(e.to)?.seen
      let d, lx = 0, ly = 0

      if (sn?.kind === 'decision' && e.label === '是') {
        const ex = s.bot, en = t.top
        if (Math.abs(ex.x - en.x) < 2) {
          d = `M${ex.x} ${ex.y + p} L${en.x} ${en.y - p}`
          lx = ex.x + 10; ly = (ex.y + en.y) / 2
        } else {
          const my = Math.round((ex.y + en.y) / 2)
          d = `M${ex.x} ${ex.y + p} L${ex.x} ${my} L${en.x} ${my} L${en.x} ${en.y - p}`
          lx = (ex.x + en.x) / 2; ly = my - 6
        }
      } else if (sn?.kind === 'decision' && e.label === '否') {
        const goL = t.cx < s.cx
        const ex = goL ? s.left : s.right
        const en = t.top
        const dir = goL ? -1 : 1
        d = `M${ex.x + dir * p} ${ex.y} L${en.x} ${ex.y} L${en.x} ${en.y - p}`
        lx = (ex.x + en.x) / 2; ly = ex.y - 8
      } else if (t.row <= s.row) {
        const ex = s.right, en = t.right
        const loopX = Math.round(Math.max(ex.x, en.x) + 16)
        d = `M${ex.x + p} ${ex.y} L${loopX} ${ex.y} L${loopX} ${en.y} L${en.x + p} ${en.y}`
        lx = loopX + 5; ly = Math.round((ex.y + en.y) / 2)
      } else {
        const ex = s.bot, en = t.top
        if (Math.abs(ex.x - en.x) < 2) {
          d = `M${ex.x} ${ex.y + p} L${en.x} ${en.y - p}`
        } else {
          const my = Math.round((ex.y + en.y) / 2)
          d = `M${ex.x} ${ex.y + p} L${ex.x} ${my} L${en.x} ${my} L${en.x} ${en.y - p}`
        }
        lx = 0; ly = 0
      }
      return { d, done, label: e.label, lx, ly, key: `${e.from}-${e.to}-${i}` }
    }).filter(Boolean)
  }, [graph.edges, pos, nodeMap, status, c])

  function nc(id) {
    let s = ''
    if (isDone(id)) s += ' slad-n--done'
    if (isCur(id)) s += ' slad-n--cur'
    if (isFail(id)) s += ' slad-n--fail'
    if (isOk(id)) s += ' slad-n--ok'
    return s
  }

  return (
    <div ref={wrapRef} className={`slad${compact ? ' slad--compact' : ''}`}>
      <div className="slad-top">
        <div className="slad-title">{title}</div>
        <div className="slad-sub">蓝色 = 已完成 · 灰色 = 未执行 · 高亮 = 当前步骤</div>
      </div>
      <div className="slad-body" style={{ '--slad-lanes': lanes.length, '--slad-col-w': `${c.colW}px` }}>
        <div className="slad-lane-hdr">
          {lanes.map(l => <div key={l.id} className="slad-lh">{l.name}</div>)}
        </div>
        <div className="slad-canvas" style={{ minHeight: svgH, minWidth: svgW }}>
          <div className="slad-cols" aria-hidden>
            {lanes.map((l, i) => <div key={l.id} className="slad-col" style={{ left: i * c.colW, width: c.colW }} />)}
          </div>
          <svg className="slad-svg" width={svgW} height={svgH} viewBox={`0 0 ${svgW} ${svgH}`}>
            <defs>
              <marker id="ah" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto" markerUnits="strokeWidth">
                <path d="M0 .5 L7 3 L0 5.5Z" fill="#94a3b8" />
              </marker>
              <marker id="ahd" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto" markerUnits="strokeWidth">
                <path d="M0 .5 L7 3 L0 5.5Z" fill="#2563eb" />
              </marker>
            </defs>

            {edgePaths.map(ep => (
              <g key={ep.key}>
                <path d={ep.d} className={`slad-e${ep.done ? ' slad-e--done' : ''}`} markerEnd={`url(#${ep.done ? 'ahd' : 'ah'})`} />
                {ep.label ? <text x={ep.lx} y={ep.ly} className={`slad-el${ep.done ? ' slad-el--done' : ''}`} textAnchor="middle" dominantBaseline="auto">{ep.label}</text> : null}
              </g>
            ))}

            {graph.nodes.map(n => {
              const b = pos.get(n.id)
              if (!b) return null
              const g = nc(n.id)
              if (n.kind === 'decision') {
                return (
                  <g key={n.id} className={`slad-n slad-n--dec${g}`}>
                    <polygon points={`${b.cx},${b.top.y} ${b.right.x},${b.cy} ${b.cx},${b.bot.y} ${b.left.x},${b.cy}`} />
                    <text x={b.cx} y={b.cy + 1} textAnchor="middle" dominantBaseline="central">{n.label}</text>
                  </g>
                )
              }
              if (n.kind === 'terminator') {
                return (
                  <g key={n.id} className={`slad-n slad-n--term${g}`}>
                    <rect x={b.cx - b.w / 2} y={b.cy - b.h / 2} width={b.w} height={b.h} rx={b.h / 2} />
                    <text x={b.cx} y={b.cy + 1} textAnchor="middle" dominantBaseline="central">{n.label}</text>
                  </g>
                )
              }
              return (
                <g key={n.id} className={`slad-n slad-n--act${g}`}>
                  <rect x={b.cx - b.w / 2} y={b.cy - b.h / 2} width={b.w} height={b.h} rx={10} />
                  <text x={b.cx} y={b.cy + 1} textAnchor="middle" dominantBaseline="central">{n.label}</text>
                </g>
              )
            })}
          </svg>
        </div>
      </div>
    </div>
  )
}
