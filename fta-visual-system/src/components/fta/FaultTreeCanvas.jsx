import { useEffect, useMemo, useState, useCallback } from 'react'
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  Panel,
  applyEdgeChanges,
  applyNodeChanges,
  useReactFlow,
  ReactFlowProvider,
  Handle,
  Position,
  getNodesBounds,
  getViewportForBounds,
} from 'reactflow'
import { toPng } from 'html-to-image'
import 'reactflow/dist/style.css'

const EVENT_NODE_W = 140
const TYPE_LABELS = { top: '顶事件', intermediate: '中间事件', basic: '基本事件' }
const FITVIEW_PADDING = 0.15

function getErrorLevelCategory(errorLevel) {
  if (!errorLevel) return 'mild'
  if (typeof errorLevel !== 'string') return 'mild'
  const lv = errorLevel.trim()
  if (lv === '严重') return 'severe'
  if (lv === '中等') return 'moderate'
  return 'mild'
}

function formatBasicLabel(text) {
  if (!text) return [text]
  const display = text.length > 16 ? text.slice(0, 15) + '…' : text
  const chunks = []
  for (let i = 0; i < display.length; i += 4) {
    chunks.push(display.slice(i, i + 4))
  }
  return chunks
}

function EventNode({ data }) {
  const cls = `fta-node fta-node--${data.type || 'event'}`
  const isBasic = data.type === 'basic'
  const typeTag = TYPE_LABELS[data.type] || ''

  const errorLevel = data.meta?.event?.errorLevel
  const viewMode = data.viewMode || 'type'
  const shouldColorByErrorLevel = viewMode === 'errorLevel' && data.type !== 'top'
  const errorCategory = shouldColorByErrorLevel
    ? getErrorLevelCategory(errorLevel)
    : null
  const errorBgClass = errorCategory ? `fta-errorLevel-bg--${errorCategory}` : ''

  return (
    <div className="fta-node-container">
      <Handle type="target" position={Position.Top} className="fta-handle" />
      <div className={`${cls}${errorBgClass ? ` ${errorBgClass}` : ''}`}>
        {typeTag && <div className="fta-node__type-tag">{typeTag}</div>}
        <div className="fta-node__label">
          {isBasic
            ? formatBasicLabel(data.label).map((chunk, i, arr) => (
                <span key={i}>
                  {chunk}
                  {i < arr.length - 1 && <br />}
                </span>
              ))
            : data.label}
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} className="fta-handle" />
    </div>
  )
}

function GateNode({ data }) {
  const isAnd = data.label === 'AND'
  const isDark = data.theme === 'dark'

  const shapeStyle = isDark
    ? {
        fill: '#111827',
        stroke: '#6C9AF0',
        strokeWidth: 2.5,
      }
    : {
        fill: isAnd ? '#fffbeb' : '#faf5ff',
        stroke: isAnd ? '#f59e0b' : '#a855f7',
        strokeWidth: 2.5,
      }

  const textStyle = isDark
    ? {
        fill: '#E5E7EB',
        fontSize: '15px',
        fontWeight: 700,
        fontFamily: 'SourceHanSerifCN, system-ui, -apple-system, sans-serif',
      }
    : {
        fill: isAnd ? '#92400e' : '#6b21a8',
        fontSize: '15px',
        fontWeight: 700,
        fontFamily: 'SourceHanSerifCN, system-ui, -apple-system, sans-serif',
      }
  return (
    <div className={`fta-gate ${isAnd ? 'fta-gate--and' : 'fta-gate--or'}`}>
      <Handle type="target" position={Position.Top} className="fta-handle" />
      <svg viewBox="0 0 60 52" width="60" height="52">
        {isAnd ? (
          <path d="M 2 52 L 2 22 C 2 0, 58 0, 58 22 L 58 52 Z" style={shapeStyle} />
        ) : (
          <path
            d="M 30 0 Q 2 10, 2 30 L 2 44 Q 30 36, 58 44 L 58 30 Q 58 10, 30 0 Z"
            style={shapeStyle}
          />
        )}
        <text
          x="30"
          y={isAnd ? 38 : 24}
          textAnchor="middle"
          dominantBaseline="middle"
          style={textStyle}
        >
          {data.label}
        </text>
      </svg>
      <Handle type="source" position={Position.Bottom} className="fta-handle" />
    </div>
  )
}

const nodeTypes = {
  eventNode: EventNode,
  gateNode: GateNode,
}

function buildLayout(nodes, edges) {
  if (!nodes.length) return { rfNodes: [], rfEdges: [] }

  const childrenOf = new Map()
  nodes.forEach((n) => childrenOf.set(n.id, []))
  edges.forEach((e) => {
    if (childrenOf.has(e.target)) {
      childrenOf.get(e.target).push(e.source)
    }
  })

  // 基础水平间距：略大于节点宽度，避免节点边缘轻微重叠
  const H_GAP = 160
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
      kids.length === 0
        ? H_GAP
        : kids.reduce((s, k) => s + getWidth(k), 0)
    widthOf.set(id, Math.max(H_GAP, w))
    return widthOf.get(id)
  }

  // 预先为所有节点计算子树宽度
  nodes.forEach((n) => {
    getWidth(n.id)
  })

  const allSources = new Set(edges.map((e) => e.source))

  // 根节点选择策略：
  // 1）若存在多个顶事件，优先选“子树宽度最大”的顶事件（通常是主故障树）
  // 2）否则，选没有父节点的事件
  // 3）再否则，退回第一个节点
  const topNodes = nodes.filter((n) => n.type === 'top')
  let root
  if (topNodes.length > 0) {
    root = topNodes.reduce((best, n) => {
      const bw = widthOf.get(best.id) || H_GAP
      const nw = widthOf.get(n.id) || H_GAP
      return nw > bw ? n : best
    }, topNodes[0])
  } else {
    root =
      nodes.find((n) => !allSources.has(n.id)) ||
      nodes[0]
  }

  const centerMap = new Map()
  function layout(id, yOffset, left) {
    const w = widthOf.get(id) || H_GAP
    centerMap.set(id, { cx: left + w / 2, cy: yOffset })
    const node = nodeMap.get(id)
    const isGate = node && node.type === 'gate'
    // 垂直间距：事件节点与其子节点间留出更长线段，避免“父-子直连”时线段被子节点遮挡
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

  // 对于未从根可达的节点（多个连通分量、孤立节点），
  // 在主树下方按“类型分行、同类型横向排布”的方式尽量分散：
  // 顶事件一排、中间事件一排、基本事件一排。
  const placedIds = new Set(centerMap.keys())
  if (placedIds.size < nodes.length) {
    let maxCy = 0
    centerMap.forEach((c) => {
      if (c.cy > maxCy) maxCy = c.cy
    })

    const rows = {
      top: [],
      intermediate: [],
      basic: [],
      other: [],
    }

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
        const cx = startX + idx * colGapX
        centerMap.set(n.id, { cx, cy })
      })
      rowIndex += 1
    })
  }

  // 简单的防遮挡：同一层(y)上若多个节点计算到几乎相同的 cx，则做轻微水平错位
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

  const rfNodes = nodes.map((n) => {
    const c = adjustedCenters.get(n.id) || centerMap.get(n.id)
    const isGate = n.type === 'gate'
    const halfW = EVENT_NODE_W / 2
    return {
      id: n.id,
      data: {
        label: n.label,
        type: n.type,
        meta: n.meta,
      },
      position: { x: c.cx - halfW, y: c.cy },
      type: isGate ? 'gateNode' : 'eventNode',
    }
  })

  const rfEdges = edges.map((e) => {
    const rfSource = e.target
    const rfTarget = e.source
    const sourceNode = nodeMap.get(rfSource)
    const isFromGate = sourceNode && sourceNode.type === 'gate'
    return {
      id: e.id || `${e.source}-${e.target}`,
      source: rfSource,
      target: rfTarget,
      type: isFromGate ? 'smoothstep' : 'straight',
      pathOptions: isFromGate ? { borderRadius: 0 } : undefined,
      animated: false,
      style: { strokeWidth: 2, stroke: '#64748b' },
    }
  })

  return { rfNodes, rfEdges }
}

function FitViewButton({ resetLayout, onResetViewFlag }) {
  const { fitView } = useReactFlow()
  return (
    <Panel position="top-right">
      <button
        type="button"
        className="fta-btn primary fta-fitview-btn"
        onClick={() => {
          resetLayout()
          onResetViewFlag?.()
          setTimeout(() => {
            fitView({
              padding: FITVIEW_PADDING,
              duration: 350,
              maxZoom: 1.5,
              minZoom: 0.1,
            })
          }, 50)
        }}
      >
        自动调整视图
      </button>
    </Panel>
  )
}

function LegendPanel({ viewMode = 'type' }) {
  const [open, setOpen] = useState(false)
  return (
    <Panel position="bottom-left">
      {open && (
        <div className="fta-legend">
          <div className="fta-legend-title">图例</div>
          <div className="fta-legend-items">
            {viewMode === 'errorLevel' ? (
              
               
              
              <div className="fta-legend-item" style={{ alignItems: 'flex-start' }}>
                 
                <div style={{ display: 'flex', gap: '0.6rem', alignItems: 'center' }}>
                  <div
                    style={{
                      width: '26px',
                      height: '92px',
                      border: '1px solid var(--fta-border)',
                      borderRadius: '6px',
                      overflow: 'hidden',
                      display: 'flex',
                      flexDirection: 'column',
                    }}
                  >
                    <div
                      className="fta-errorLevel-bar--severe"
                      style={{ flex: 1 }}
                    />
                    <div
                      className="fta-errorLevel-bar--moderate"
                      style={{ flex: 1 }}
                    />
                    <div className="fta-errorLevel-bar--mild" style={{ flex: 1 }} />
                  </div>
                  <div
                    style={{
                      display: 'flex',
                      flexDirection: 'column',
                      height: '92px',
                      justifyContent: 'space-between',
                      paddingTop: '0.05rem',
                    }}
                  >
                    <span>严重</span>
                    <span>中等</span>
                    <span>轻微</span>
                  </div>
                </div>
              </div>
            ) : (
              <>
                <div className="fta-legend-item">
                  <span className="fta-legend-icon fta-legend-icon--top" />
                  <span>顶事件</span>
                </div>
                <div className="fta-legend-item">
                  <span className="fta-legend-icon fta-legend-icon--intermediate" />
                  <span>中间事件</span>
                </div>
                <div className="fta-legend-item">
                  <span className="fta-legend-icon fta-legend-icon--basic" />
                  <span>基本事件</span>
                </div>
              </>
            )}
            {viewMode === 'errorLevel' && (
              <div className="fta-legend-item">
                <span className="fta-legend-icon fta-legend-icon--top" />
                <span>顶事件</span>
              </div>
            )}
            <div className="fta-legend-item">
              <svg viewBox="0 0 60 52" width="28" height="24">
                <path d="M 2 52 L 2 22 C 2 0, 58 0, 58 22 L 58 52 Z"
                  style={{ fill: '#fffbeb', stroke: '#f59e0b', strokeWidth: 3 }} />
                <text x="30" y="38" textAnchor="middle" dominantBaseline="middle"
                  style={{ fill: '#92400e', fontSize: '14px', fontWeight: 700 }}>AND</text>
              </svg>
              <span>与门（AND）</span>
            </div>
            <div className="fta-legend-item">
              <svg viewBox="0 0 60 52" width="28" height="24">
                <path d="M 30 0 Q 2 10, 2 30 L 2 44 Q 30 36, 58 44 L 58 30 Q 58 10, 30 0 Z"
                  style={{ fill: '#faf5ff', stroke: '#a855f7', strokeWidth: 3 }} />
                <text x="30" y="24" textAnchor="middle" dominantBaseline="middle"
                  style={{ fill: '#6b21a8', fontSize: '14px', fontWeight: 700 }}>OR</text>
              </svg>
              <span>或门（OR）</span>
            </div>
          </div>
        </div>
      )}
      <button
        type="button"
        className="fta-legend-toggle"
        onClick={() => setOpen((v) => !v)}
        title="图例"
      >
        ?
      </button>
    </Panel>
  )
}

function CanvasInner({
  graphData,
  onNodeSelect,
  onNodeContextMenu,
  onPaneContextMenu,
  onNodeDoubleClick,
  onConnectEdge,
  onEdgeContextMenu,
  showChrome = true,
  canvasActionsRef,
  theme = 'light',
  viewMode = 'type',
}) {
  const init = useMemo(() => {
    const layout = buildLayout(graphData.nodes || [], graphData.edges || [])
    return {
      rfNodes: layout.rfNodes.map((n) => ({
        ...n,
        data: {
          ...n.data,
          theme,
          viewMode,
        },
      })),
      rfEdges: layout.rfEdges,
    }
  }, [graphData, theme, viewMode])

  const [nodes, setNodes] = useState(init.rfNodes)
  const [edges, setEdges] = useState(init.rfEdges)
  const [userAdjustedView, setUserAdjustedView] = useState(false)
  const { getNodes, fitView } = useReactFlow()

  useEffect(() => {
    setNodes(init.rfNodes)
    setEdges(init.rfEdges)
    if (!userAdjustedView) {
      setTimeout(
        () => fitView({ padding: FITVIEW_PADDING, duration: 350 }),
        80,
      )
    }
  }, [init, fitView, userAdjustedView])

  const exportImage = useCallback(async () => {
    const currentNodes = getNodes()
    if (!currentNodes.length) return null
    const bounds = getNodesBounds(currentNodes)
    const PADDING = 60
    const w = Math.ceil(bounds.width + PADDING * 2)
    const h = Math.ceil(bounds.height + PADDING * 2)
    const vp = getViewportForBounds(bounds, w, h, 0.5, 2)
    const el = document.querySelector('.react-flow__viewport')
    if (!el) return null

    // 按当前画布主题使用相同背景色，其他样式与画布保持一致
    const bg = theme === 'dark' ? '#4D4D4D' : '#F8F0F2'

    return toPng(el, {
      backgroundColor: bg,
      width: w,
      height: h,
      pixelRatio: 3,
      style: {
        width: `${w}px`,
        height: `${h}px`,
        transform: `translate(${vp.x}px, ${vp.y}px) scale(${vp.zoom})`,
      },
    })
  }, [getNodes, theme])

  if (canvasActionsRef) {
    canvasActionsRef.current = { exportImage }
  }

  const handleNodeCtx = useCallback(
    (event, node) => {
      event.preventDefault()
      onNodeContextMenu?.(event, node)
    },
    [onNodeContextMenu],
  )

  const handlePaneCtx = useCallback(
    (event) => {
      event.preventDefault()
      onPaneContextMenu?.(event)
    },
    [onPaneContextMenu],
  )

  const handleDblClick = useCallback(
    (event, node) => {
      onNodeDoubleClick?.(event, node)
    },
    [onNodeDoubleClick],
  )

  const handleEdgeCtx = useCallback(
    (event, edge) => {
      event.preventDefault()
      onEdgeContextMenu?.(event, edge)
    },
    [onEdgeContextMenu],
  )

  const handleMove = useCallback(() => {
    setUserAdjustedView(true)
  }, [])

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodesChange={(ch) => setNodes((ns) => applyNodeChanges(ch, ns))}
      onEdgesChange={(ch) => setEdges((es) => applyEdgeChanges(ch, es))}
      onNodeClick={(_, node) => onNodeSelect?.(node)}
      onNodeContextMenu={handleNodeCtx}
      onPaneContextMenu={handlePaneCtx}
      onNodeDoubleClick={handleDblClick}
      onConnect={onConnectEdge}
      onEdgeContextMenu={handleEdgeCtx}
      onMove={handleMove}
      onMoveStart={handleMove}
      onMoveEnd={handleMove}
      fitView
      fitViewOptions={{ padding: FITVIEW_PADDING }}
      proOptions={{ hideAttribution: true }}
      className="fta-reactflow"
    >
      <Background
        color={theme === 'dark' ? '#d1d5db' : '#e2e8f0'}
        gap={20}
        variant="dots"
      />
      {showChrome && (
        <>
          <MiniMap nodeColor={() => '#818cf8'} maskColor="rgba(255,255,255,0.7)" />
          <Controls showInteractive={false} position="top-left" />
          <FitViewButton
            resetLayout={() => {
              setNodes(init.rfNodes)
              setEdges(init.rfEdges)
            }}
            onResetViewFlag={() => setUserAdjustedView(false)}
          />
          <LegendPanel viewMode={viewMode} />
        </>
      )}
    </ReactFlow>
  )
}

export default function FaultTreeCanvas({
  canvasActionsRef,
  theme = 'light',
  viewMode = 'type',
  ...props
}) {
  return (
    <ReactFlowProvider>
      <CanvasInner
        {...props}
        canvasActionsRef={canvasActionsRef}
        theme={theme}
        viewMode={viewMode}
      />
    </ReactFlowProvider>
  )
}
