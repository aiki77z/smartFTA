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
const GATE_NODE_W = 60

function formatBasicLabel(text) {
  if (!text) return [text]
  const display = text.length > 16 ? text.slice(0, 15) + '…' : text
  const chunks = []
  for (let i = 0; i < display.length; i += 4) {
    chunks.push(display.slice(i, i + 4))
  }
  return chunks
}

/* ────────────────────────────────────────────
   Custom Node: Event (top / intermediate / basic)
   ──────────────────────────────────────────── */
function EventNode({ data }) {
  const cls = `fta-node fta-node--${data.type || 'event'}`
  const isBasic = data.type === 'basic'
  return (
    <div className="fta-node-container">
      <Handle type="target" position={Position.Top} className="fta-handle" />
      <div className={cls}>
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

/* ────────────────────────────────────────────
   Custom Node: Gate (AND / OR) with inline SVG
   Inline styles ensure html-to-image export works
   ──────────────────────────────────────────── */
function GateNode({ data }) {
  const isAnd = data.label === 'AND'
  const shapeStyle = {
    fill: isAnd ? '#fffbeb' : '#faf5ff',
    stroke: isAnd ? '#f59e0b' : '#a855f7',
    strokeWidth: 2.5,
  }
  const textStyle = {
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

/* ────────────────────────────────────────────
   Layout
   ──────────────────────────────────────────── */
function stripGatePrefix(label) {
  return label.replace(/^\[(AND|OR)\]\s*/, '')
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

  const allSources = new Set(edges.map((e) => e.source))
  const root =
    nodes.find((n) => n.type === 'top') ||
    nodes.find((n) => !allSources.has(n.id)) ||
    nodes[0]

  const H_GAP = 120
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
  getWidth(root.id)

  const centerMap = new Map()
  function layout(id, yOffset, left) {
    const w = widthOf.get(id) || H_GAP
    centerMap.set(id, { cx: left + w / 2, cy: yOffset })
    const node = nodeMap.get(id)
    const isGate = node && node.type === 'gate'
    const vGap = isGate ? 100 : 70
    let cur = left
    ;(childrenOf.get(id) || []).forEach((kid) => {
      const kw = widthOf.get(kid) || H_GAP
      layout(kid, yOffset + vGap, cur)
      cur += kw
    })
  }
  const rootW = widthOf.get(root.id) || H_GAP
  layout(root.id, 0, -rootW / 2)

  nodes.forEach((n) => {
    if (!centerMap.has(n.id)) {
      const pos = n.position || { x: 0, y: 0 }
      centerMap.set(n.id, { cx: pos.x, cy: pos.y })
    }
  })

  const rfNodes = nodes.map((n) => {
    const c = centerMap.get(n.id)
    const isGate = n.type === 'gate'
    const halfW = EVENT_NODE_W / 2
    return {
      id: n.id,
      data: {
        label: stripGatePrefix(n.label),
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

/* ────────────────────────────────────────────
   Fit-view button
   ──────────────────────────────────────────── */
function FitViewButton({ resetLayout }) {
  const { fitView } = useReactFlow()
  return (
    <Panel position="top-right">
      <button
        type="button"
        className="fta-btn primary fta-fitview-btn"
        onClick={() => {
          resetLayout()
          setTimeout(() => {
            fitView({ padding: 0.2, duration: 300, maxZoom: 1.5, minZoom: 0.1 })
          }, 50)
        }}
      >
        自动调整视图
      </button>
    </Panel>
  )
}

/* ────────────────────────────────────────────
   Legend panel
   ──────────────────────────────────────────── */
function LegendPanel() {
  const [open, setOpen] = useState(false)
  return (
    <Panel position="bottom-left">
      {open && (
        <div className="fta-legend">
          <div className="fta-legend-title">图例</div>
          <div className="fta-legend-items">
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

/* ────────────────────────────────────────────
   Inner canvas
   ──────────────────────────────────────────── */
function CanvasInner({ graphData, onNodeSelect, showChrome = true, canvasActionsRef }) {
  const init = useMemo(
    () => buildLayout(graphData.nodes || [], graphData.edges || []),
    [graphData.nodes, graphData.edges],
  )

  const [nodes, setNodes] = useState(init.rfNodes)
  const [edges, setEdges] = useState(init.rfEdges)
  const { getNodes } = useReactFlow()

  useEffect(() => {
    setNodes(init.rfNodes)
    setEdges(init.rfEdges)
  }, [init.rfNodes, init.rfEdges])

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
    return toPng(el, {
      backgroundColor: '#F8F0F2',
      width: w,
      height: h,
      pixelRatio: 3,
      style: {
        width: `${w}px`,
        height: `${h}px`,
        transform: `translate(${vp.x}px, ${vp.y}px) scale(${vp.zoom})`,
      },
    })
  }, [getNodes])

  if (canvasActionsRef) {
    canvasActionsRef.current = { exportImage }
  }

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodesChange={(ch) => setNodes((ns) => applyNodeChanges(ch, ns))}
      onEdgesChange={(ch) => setEdges((es) => applyEdgeChanges(ch, es))}
      onNodeClick={(_, node) => onNodeSelect?.(node)}
      fitView
      fitViewOptions={{ padding: 0.2 }}
      proOptions={{ hideAttribution: true }}
      className="fta-reactflow"
    >
      <Background color="#e2e8f0" gap={20} />
      {showChrome && (
        <>
          <MiniMap
            nodeColor={() => '#818cf8'}
            maskColor="rgba(255,255,255,0.7)"
          />
          <Controls showInteractive={false} position="top-left" />
          <FitViewButton resetLayout={() => {
            setNodes(init.rfNodes)
            setEdges(init.rfEdges)
          }} />
          <LegendPanel />
        </>
      )}
    </ReactFlow>
  )
}

/* ────────────────────────────────────────────
   Exported wrapper
   ──────────────────────────────────────────── */
export default function FaultTreeCanvas({ canvasActionsRef, ...props }) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} canvasActionsRef={canvasActionsRef} />
    </ReactFlowProvider>
  )
}
