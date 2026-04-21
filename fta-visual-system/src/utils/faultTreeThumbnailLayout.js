/**
 * 与 FaultTreeCanvas.jsx 中 buildLayout 同源：用于首页卡片缩略图，
 * 使缩略图拓扑与画布内故障树一致（树形排布、门节点、孤立分量分行等）。
 */
const H_GAP = 160
export const EVENT_NODE_W = 140
const EVENT_BOX_H = 56
const GATE_W = 60
const GATE_H = 52
const BASIC_DIAM = 110

function getNodeFootprint(n) {
  if (!n) return { w: EVENT_NODE_W, h: EVENT_BOX_H, footCxOfs: 0 }
  if (n.type === 'gate') return { w: GATE_W, h: GATE_H, footCxOfs: 0 }
  if (n.type === 'basic') return { w: BASIC_DIAM, h: BASIC_DIAM, footCxOfs: 0 }
  return { w: EVENT_NODE_W, h: EVENT_BOX_H, footCxOfs: 0 }
}

/** @returns {Map<string, { cx: number, cy: number }>} */
function computeAdjustedCenters(nodes, edges) {
  if (!nodes.length) return new Map()

  const childrenOf = new Map()
  nodes.forEach((n) => childrenOf.set(n.id, []))
  edges.forEach((e) => {
    if (childrenOf.has(e.target)) {
      childrenOf.get(e.target).push(e.source)
    }
  })

  const nodeMap = new Map(nodes.map((n) => [n.id, n]))
  const widthOf = new Map()
  const visitedW = new Set()

  function getWidth(id) {
    if (widthOf.has(id)) return widthOf.get(id)
    if (visitedW.has(id)) {
      widthOf.set(id, H_GAP)
      return H_GAP
    }
    visitedW.add(id)
    const kids = childrenOf.get(id) || []
    const w =
      kids.length === 0 ? H_GAP : kids.reduce((s, k) => s + getWidth(k), 0)
    widthOf.set(id, Math.max(H_GAP, w))
    return widthOf.get(id)
  }

  nodes.forEach((n) => {
    getWidth(n.id)
  })

  const allSources = new Set(edges.map((e) => e.source))
  const topNodes = nodes.filter((n) => n.type === 'top')
  let root
  if (topNodes.length > 0) {
    root = topNodes.reduce((best, n) => {
      const bw = widthOf.get(best.id) || H_GAP
      const nw = widthOf.get(n.id) || H_GAP
      return nw > bw ? n : best
    }, topNodes[0])
  } else {
    root = nodes.find((n) => !allSources.has(n.id)) || nodes[0]
  }

  const centerMap = new Map()
  function layout(id, yOffset, left) {
    const w = widthOf.get(id) || H_GAP
    centerMap.set(id, { cx: left + w / 2, cy: yOffset })
    const node = nodeMap.get(id)
    const isGate = node && node.type === 'gate'
    const vGap = isGate ? 100 : 90
    let cur = left
    ;(childrenOf.get(id) || []).forEach((kid) => {
      const kw = widthOf.get(kid) || H_GAP
      layout(kid, yOffset + vGap, cur)
      cur += kw
    })
  }
  const rootW = widthOf.get(root.id) || H_GAP
  layout(root.id, 0, -rootW / 2)

  const placedIds = new Set(centerMap.keys())
  if (placedIds.size < nodes.length) {
    let maxCy = 0
    centerMap.forEach((c) => {
      if (c.cy > maxCy) maxCy = c.cy
    })

    const rows = { top: [], intermediate: [], basic: [], other: [] }
    nodes.forEach((n) => {
      if (centerMap.has(n.id)) return
      if (n.type === 'top') rows.top.push(n)
      else if (n.type === 'intermediate') rows.intermediate.push(n)
      else if (n.type === 'basic') rows.basic.push(n)
      else rows.other.push(n)
    })

    const rowOrder = ['top', 'intermediate', 'basic', 'other']
    const rowGapY = 140
    const colGapX = 200
    let rowIndex = 0
    rowOrder.forEach((key) => {
      const list = rows[key]
      if (!list.length) return
      const cy = maxCy + 200 + rowIndex * rowGapY
      const totalWidth = (list.length - 1) * colGapX
      const startX = -totalWidth / 2
      list.forEach((n, idx) => {
        centerMap.set(n.id, { cx: startX + idx * colGapX, cy })
      })
      rowIndex += 1
    })
  }

  const usedSlotsByRow = new Map()
  const adjustedCenters = new Map()
  const MIN_HORIZONTAL_GAP = EVENT_NODE_W * 0.8

  centerMap.forEach((c, id) => {
    const rowKey = Math.round(c.cy / 10) * 10
    const row = usedSlotsByRow.get(rowKey) || []

    let offsetCx = c.cx
    let tries = 0
    while (
      row.some((existingCx) => Math.abs(existingCx - offsetCx) < MIN_HORIZONTAL_GAP) &&
      tries < 10
    ) {
      const direction = tries % 2 === 0 ? 1 : -1
      const step = Math.ceil(tries / 2) * (MIN_HORIZONTAL_GAP * 0.6)
      offsetCx = c.cx + direction * step
      tries += 1
    }

    row.push(offsetCx)
    usedSlotsByRow.set(rowKey, row)
    adjustedCenters.set(id, { cx: offsetCx, cy: c.cy })
  })

  return adjustedCenters
}

function nodeLeftX(n, cx) {
  if (n?.type === 'basic') return cx - BASIC_DIAM / 2
  if (n?.type === 'gate') return cx - GATE_W / 2
  return cx - EVENT_NODE_W / 2
}

function nodeRightX(n, cx) {
  if (n?.type === 'basic') return cx + BASIC_DIAM / 2
  if (n?.type === 'gate') return cx + GATE_W / 2
  return cx + EVENT_NODE_W / 2
}

/**
 * @param {{ nodes?: unknown[], edges?: unknown[] }} graphData
 * @param {{ width?: number, height?: number, pad?: number }} [opts]
 * @returns {{
 *   viewBox: string,
 *   lines: { x1: number, y1: number, x2: number, y2: number }[],
 *   shapes: { kind: string, id: string, [k: string]: unknown }[],
 * } | null}
 */
function normalizeGraphData(graphData) {
  const nodes = (graphData?.nodes || []).map((n) => ({
    ...n,
    type: n.type || n.data?.type || 'intermediate',
    label: n.label ?? n.data?.label ?? '',
  }))
  const edges = graphData?.edges || []
  return { nodes, edges }
}

export function buildFaultTreeThumbnailScene(graphData, opts = {}) {
  const { nodes, edges } = normalizeGraphData(graphData)
  if (!nodes.length) return null

  const TW = opts.width ?? 120
  const TH = opts.height ?? 80
  const PAD = opts.pad ?? 6

  const nodeMap = new Map(nodes.map((n) => [n.id, n]))
  const centers = computeAdjustedCenters(nodes, edges)

  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity

  nodes.forEach((n) => {
    const c = centers.get(n.id)
    if (!c) return
    const left = nodeLeftX(n, c.cx)
    const right = nodeRightX(n, c.cx)
    const { h } = getNodeFootprint(n)
    minX = Math.min(minX, left)
    maxX = Math.max(maxX, right)
    minY = Math.min(minY, c.cy)
    maxY = Math.max(maxY, c.cy + h)
  })

  if (!Number.isFinite(minX)) return null

  const bw = Math.max(maxX - minX, 1)
  const bh = Math.max(maxY - minY, 1)
  const innerW = TW - 2 * PAD
  const innerH = TH - 2 * PAD
  const scale = Math.min(innerW / bw, innerH / bh)

  const tx = (x) => PAD + (x - minX) * scale
  const ty = (y) => PAD + (y - minY) * scale

  const lines = []
  for (const e of edges) {
    const child = centers.get(e.source)
    const parent = centers.get(e.target)
    const nc = nodeMap.get(e.source)
    const np = nodeMap.get(e.target)
    if (!child || !parent || !nc || !np) continue
    const ch = getNodeFootprint(nc).h
    const x1 = tx(child.cx)
    const y1 = ty(child.cy + ch)
    const x2 = tx(parent.cx)
    const y2 = ty(parent.cy)
    lines.push({ x1, y1, x2, y2 })
  }

  const shapes = []
  nodes.forEach((n) => {
    const c = centers.get(n.id)
    if (!c) return
    const left = nodeLeftX(n, c.cx)
    const top = c.cy
    const { w, h } = getNodeFootprint(n)
    if (n.type === 'basic') {
      shapes.push({
        kind: 'ellipse',
        id: n.id,
        cx: tx(c.cx),
        cy: ty(c.cy + h / 2),
        rx: (w / 2) * scale,
        ry: (h / 2) * scale,
        nodeType: 'basic',
      })
    } else if (n.type === 'gate') {
      const isAnd = n.label === 'AND'
      shapes.push({
        kind: 'gate',
        id: n.id,
        x: tx(left),
        y: ty(top),
        w: w * scale,
        h: h * scale,
        isAnd,
      })
    } else {
      shapes.push({
        kind: 'rect',
        id: n.id,
        x: tx(left),
        y: ty(top),
        w: w * scale,
        h: h * scale,
        rx: 3 * Math.min(scale, 1.2),
        nodeType: n.type || 'intermediate',
      })
    }
  })

  return {
    viewBox: `0 0 ${TW} ${TH}`,
    lines,
    shapes,
  }
}
