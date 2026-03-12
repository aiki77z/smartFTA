import { useEffect, useMemo, useState } from 'react'
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
} from 'reactflow'
import 'reactflow/dist/style.css'

const EVENT_NODE_W = 140
const GATE_NODE_W = 60

/* ────────────────────────────────────────────
   Custom Node: Event (top / intermediate / basic)
   ──────────────────────────────────────────── */
function EventNode({ data }) {
  const cls = `fta-node fta-node--${data.type || 'event'}`
  return (
    <div className={cls}>
      <Handle type="target" position={Position.Top} className="fta-handle" />
      <div className="fta-node__label">{data.label}</div>
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
    fontSize: '13px',
    fontWeight: 700,
    fontFamily: 'system-ui, -apple-system, sans-serif',
  }
  return (
    <div className={`fta-gate ${isAnd ? 'fta-gate--and' : 'fta-gate--or'}`}>
      <Handle type="target" position={Position.Top} className="fta-handle" />
      <svg viewBox="0 0 60 52" width="60" height="52">
        {isAnd ? (
          <path d="M 5 48 L 5 24 C 5 4, 55 4, 55 24 L 55 48 Z" style={shapeStyle} />
        ) : (
          <path
            d="M 30 2 Q 5 10, 5 28 L 5 38 Q 30 32, 55 38 L 55 28 Q 55 10, 30 2 Z"
            style={shapeStyle}
          />
        )}
        <text
          x="30"
          y={isAnd ? 38 : 28}
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

  const H_GAP = 200
  const V_GAP = 110
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
  function layout(id, depth, left) {
    const w = widthOf.get(id) || H_GAP
    centerMap.set(id, { cx: left + w / 2, cy: depth * V_GAP })
    let cur = left
    ;(childrenOf.get(id) || []).forEach((kid) => {
      const kw = widthOf.get(kid) || H_GAP
      layout(kid, depth + 1, cur)
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

  const nodeMap = new Map(nodes.map((n) => [n.id, n]))

  const rfNodes = nodes.map((n) => {
    const c = centerMap.get(n.id)
    const isGate = n.type === 'gate'
    const halfW = isGate ? GATE_NODE_W / 2 : EVENT_NODE_W / 2
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

  const rfEdges = edges.map((e) => ({
    id: e.id || `${e.source}-${e.target}`,
    source: e.target,
    target: e.source,
    type: 'smoothstep',
    animated: false,
    style: { strokeWidth: 2, stroke: '#64748b' },
  }))

  return { rfNodes, rfEdges }
}

/* ────────────────────────────────────────────
   Fit-view button
   ──────────────────────────────────────────── */
function FitViewButton() {
  const { fitView } = useReactFlow()
  return (
    <Panel position="top-right">
      <button
        type="button"
        className="fta-btn primary fta-fitview-btn"
        onClick={() => fitView({ padding: 0.2, duration: 300 })}
      >
        自动调整视图
      </button>
    </Panel>
  )
}

/* ────────────────────────────────────────────
   Inner canvas
   ──────────────────────────────────────────── */
function CanvasInner({ graphData, onNodeSelect, showChrome = true }) {
  const init = useMemo(
    () => buildLayout(graphData.nodes || [], graphData.edges || []),
    [graphData.nodes, graphData.edges],
  )

  const [nodes, setNodes] = useState(init.rfNodes)
  const [edges, setEdges] = useState(init.rfEdges)

  useEffect(() => {
    setNodes(init.rfNodes)
    setEdges(init.rfEdges)
  }, [init.rfNodes, init.rfEdges])

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
          <Controls showInteractive={false} />
          <FitViewButton />
        </>
      )}
    </ReactFlow>
  )
}

/* ────────────────────────────────────────────
   Exported wrapper
   ──────────────────────────────────────────── */
export default function FaultTreeCanvas(props) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  )
}
