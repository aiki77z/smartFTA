import { useEffect, useMemo, useRef, useState } from 'react'
import './swimlane-activity-diagram.css'

function textOf(e) { return String(e?.text || e?.message || '') }
function typeOf(e) { return String(e?.type || e?.agent || '').toUpperCase() }
function artifactOf(e) { return String(e?.artifactType || e?.artifact_type || e?.payload?.artifact_type || e?.payload?.type || '').toLowerCase() }
function stageOf(e) { return String(e?.stage || '').toLowerCase() }
function validationPassed(e) {
  if (e?.payload?.passed === false) return false
  if (e?.payload?.valid === false) return false
  return true
}

function findFirstSeq(events, pred) {
  for (const e of events || []) if (pred(e)) return Number(e.seq) || 0
  return null
}

function findLastSeq(events, pred) {
  let r = null
  for (const e of events || []) if (pred(e)) r = Number(e.seq) || 0
  return r
}

const C = { colW: 84, rowH: 50, nodeW: 72, nodeH: 28, dR: 22, tW: 58, tH: 24, pad: 3 }
const CC = { colW: 80, rowH: 46, nodeW: 68, nodeH: 26, dR: 20, tW: 54, tH: 22, pad: 3 }

function bounds(kind, cx, cy, c) {
  if (kind === 'decision') {
    const r = c.dR
    return {
      cx,
      cy,
      w: r * 2,
      h: r * 2,
      top: { x: cx, y: cy - r },
      bot: { x: cx, y: cy + r },
      left: { x: cx - r, y: cy },
      right: { x: cx + r, y: cy },
    }
  }
  const w = kind === 'terminator' ? c.tW : c.nodeW
  const h = kind === 'terminator' ? c.tH : c.nodeH
  return {
    cx,
    cy,
    w,
    h,
    top: { x: cx, y: cy - h / 2 },
    bot: { x: cx, y: cy + h / 2 },
    left: { x: cx - w / 2, y: cy },
    right: { x: cx + w / 2, y: cy },
  }
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
      .map(e => ({
        ...e,
        seq: Number(e.seq) || 0,
        level: String(e.level || '').toUpperCase(),
        text: String(e.text || ''),
        stage: stageOf(e),
        type: typeOf(e),
        artifactType: artifactOf(e),
      }))
      .sort((a, b) => a.seq - b.seq),
    [events])

  const lanes = useMemo(() => [
    { id: 'scheduler', name: '调度智能体' },
    { id: 'recall', name: '范围与检索智能体' },
    { id: 'llm2', name: '构建智能体' },
    { id: 'validate', name: '校验智能体' },
    { id: 'repair', name: '修复智能体' },
    { id: 'persist', name: '持久化智能体' },
  ], [])

  const laneIdx = useMemo(() => {
    const m = new Map()
    lanes.forEach((l, i) => m.set(l.id, i))
    return m
  }, [lanes])

  const c = useMemo(() => {
    const minColW = baseC.colW
    const usable = Math.max(0, Number(wrapWidth) || 0)
    const dynamicColW = lanes.length > 0 ? Math.floor(usable / lanes.length) : minColW
    return { ...baseC, colW: Math.max(minColW, dynamicColW) }
  }, [baseC, wrapWidth, lanes.length])

  const graph = useMemo(() => {
    const nodes = [
      {
        id: 'start',
        lane: 'scheduler',
        row: 0,
        kind: 'terminator',
        label: '开始',
        match: e => typeOf(e) === 'RUN_CREATED' || typeOf(e) === 'STAGE_STARTED' || textOf(e).includes('任务开始'),
      },
      {
        id: 'graph_match',
        lane: 'recall',
        row: 1,
        kind: 'action',
        label: '匹配顶事件',
        match: e =>
          stageOf(e) === 'scope' ||
          stageOf(e) === 'graph_match' ||
          typeOf(e) === 'SCOPE_RESOLVED' ||
          typeOf(e) === 'CONFIRMATION_REQUIRED' ||
          typeOf(e) === 'CONFIRMATION_RECEIVED' ||
          artifactOf(e) === 'scope' ||
          textOf(e).includes('[graph-match]') ||
          /matching top event/i.test(textOf(e)),
      },
      {
        id: 'graph_subgraph',
        lane: 'recall',
        row: 2,
        kind: 'action',
        label: '扩展局部子图',
        match: e =>
          stageOf(e) === 'retrieval' ||
          stageOf(e) === 'graph_subgraph' ||
          typeOf(e) === 'RETRIEVAL_DONE' ||
          artifactOf(e) === 'retrieval_context' ||
          textOf(e).includes('[graph-subgraph]') ||
          /expanding local graph/i.test(textOf(e)),
      },
      {
        id: 'graph_chunks',
        lane: 'recall',
        row: 3,
        kind: 'action',
        label: '召回证据 Chunk',
        match: e => stageOf(e) === 'graph_chunks' || typeOf(e) === 'RETRIEVAL_DONE' || /collecting subgraph evidence/i.test(textOf(e)),
      },
      {
        id: 'draft',
        lane: 'llm2',
        row: 4,
        kind: 'action',
        label: '生成草稿',
        match: e =>
          stageOf(e) === 'draft' ||
          stageOf(e) === 'generate_draft' ||
          typeOf(e) === 'DRAFT_GENERATED' ||
          artifactOf(e) === 'tree_draft' ||
          textOf(e).includes('生成草稿') ||
          textOf(e).includes('LLM#2') ||
          textOf(e).includes('[graph-llm]') ||
          textOf(e).includes('[graph-draft]'),
      },
      {
        id: 'chk_valid',
        lane: 'validate',
        row: 5,
        kind: 'decision',
        label: '校验通过?',
        match: e =>
          stageOf(e) === 'validate' ||
          typeOf(e) === 'VALIDATION_DONE' ||
          artifactOf(e) === 'validation_report' ||
          textOf(e).includes('校验') ||
          textOf(e).includes('[graph-validate]'),
      },
      {
        id: 'retry',
        lane: 'llm2',
        row: 6,
        kind: 'action',
        label: '重新生成',
        match: e => typeOf(e) === 'REPAIR_REQUESTED' || textOf(e).includes('重新生成') || textOf(e).includes('[graph-regenerate]'),
      },
      {
        id: 'valid_ok',
        lane: 'validate',
        row: 7,
        kind: 'action',
        label: '校验通过',
        match: e =>
          (typeOf(e) === 'VALIDATION_DONE' && validationPassed(e)) ||
          /validation passed/i.test(textOf(e)) ||
          textOf(e).includes('校验通过'),
      },
      {
        id: 'repair',
        lane: 'repair',
        row: 8,
        kind: 'action',
        label: '历史修正',
        match: e =>
          stageOf(e) === 'repair' ||
          stageOf(e) === 'fix' ||
          artifactOf(e) === 'repair_patch' ||
          textOf(e).includes('[history-repair]') ||
          textOf(e).includes('修复'),
      },
      {
        id: 'persist',
        lane: 'persist',
        row: 9,
        kind: 'action',
        label: '保存版本',
        match: e =>
          stageOf(e) === 'persistence' ||
          stageOf(e) === 'commit' ||
          typeOf(e) === 'TREE_COMMITTED' ||
          artifactOf(e) === 'final_tree' ||
          textOf(e).includes('persisting') ||
          textOf(e).includes('保存'),
      },
      {
        id: 'chk_save',
        lane: 'persist',
        row: 10,
        kind: 'decision',
        label: '成功?',
        match: e =>
          stageOf(e) === 'completed' ||
          stageOf(e) === 'failed' ||
          stageOf(e) === 'curate' ||
          typeOf(e) === 'RUN_COMPLETED' ||
          typeOf(e) === 'RUN_FAILED' ||
          textOf(e).includes('生成完成') ||
          textOf(e).includes('生成失败'),
      },
      {
        id: 'done',
        lane: 'persist',
        row: 11,
        kind: 'terminator',
        label: '完成',
        match: e => stageOf(e) === 'completed' || typeOf(e) === 'RUN_COMPLETED' || textOf(e).includes('生成完成'),
      },
      {
        id: 'failed',
        lane: 'repair',
        row: 11,
        kind: 'terminator',
        label: '失败',
        match: e => stageOf(e) === 'failed' || typeOf(e) === 'RUN_FAILED' || (String(e.level || '').toUpperCase() === 'ERROR' && textOf(e).includes('生成失败')),
      },
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

  const nodeMap = useMemo(() => {
    const m = new Map()
    graph.nodes.forEach(n => m.set(n.id, n))
    return m
  }, [graph.nodes])

  const status = useMemo(() => {
    const ni = new Map()
    for (const n of graph.nodes) {
      const f = findFirstSeq(normEv, n.match)
      const l = findLastSeq(normEv, n.match)
      ni.set(n.id, { seen: f != null, firstSeq: f, lastSeq: l })
    }
    const failedSeq = findFirstSeq(normEv, e => stageOf(e) === 'failed' || typeOf(e) === 'RUN_FAILED' || e.level === 'ERROR')
    const doneSeq = findFirstSeq(normEv, e => stageOf(e) === 'completed' || typeOf(e) === 'RUN_COMPLETED' || textOf(e).includes('生成完成'))
    const order = [
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
      for (let i = order.length - 1; i >= 0; i -= 1) {
        const id = order[i]
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
      const s = pos.get(e.from)
      const t = pos.get(e.to)
      if (!s || !t) return null
      const sn = nodeMap.get(e.from)
      const done = !!status.ni.get(e.from)?.seen && !!status.ni.get(e.to)?.seen
      let d
      let lx = 0
      let ly = 0

      if (sn?.kind === 'decision' && e.label === '是') {
        const ex = s.bot
        const en = t.top
        if (Math.abs(ex.x - en.x) < 2) {
          d = `M${ex.x} ${ex.y + p} L${en.x} ${en.y - p}`
          lx = ex.x + 10
          ly = (ex.y + en.y) / 2
        } else {
          const my = Math.round((ex.y + en.y) / 2)
          d = `M${ex.x} ${ex.y + p} L${ex.x} ${my} L${en.x} ${my} L${en.x} ${en.y - p}`
          lx = (ex.x + en.x) / 2
          ly = my - 6
        }
      } else if (sn?.kind === 'decision' && e.label === '否') {
        const goL = t.cx < s.cx
        const ex = goL ? s.left : s.right
        const en = t.top
        const dir = goL ? -1 : 1
        d = `M${ex.x + dir * p} ${ex.y} L${en.x} ${ex.y} L${en.x} ${en.y - p}`
        lx = (ex.x + en.x) / 2
        ly = ex.y - 8
      } else if (t.row <= s.row) {
        const ex = s.right
        const en = t.right
        const loopX = Math.round(Math.max(ex.x, en.x) + 16)
        d = `M${ex.x + p} ${ex.y} L${loopX} ${ex.y} L${loopX} ${en.y} L${en.x + p} ${en.y}`
        lx = loopX + 5
        ly = Math.round((ex.y + en.y) / 2)
      } else {
        const ex = s.bot
        const en = t.top
        if (Math.abs(ex.x - en.x) < 2) {
          d = `M${ex.x} ${ex.y + p} L${en.x} ${en.y - p}`
        } else {
          const my = Math.round((ex.y + en.y) / 2)
          d = `M${ex.x} ${ex.y + p} L${ex.x} ${my} L${en.x} ${my} L${en.x} ${en.y - p}`
        }
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
