import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  MarkerType,
  BaseEdge,
  EdgeLabelRenderer,
  Handle,
  Position,
  useEdgesState,
  useNodesState,
} from 'reactflow'
import 'reactflow/dist/style.css'
import './knowledge-graph-modal.css'
import { graphCypherQuery } from '../api/ftaBackend.js'

function pickNodeKind(raw) {
  const labels = Array.isArray(raw?.labels) ? raw.labels : []
  const props = raw?.props && typeof raw.props === 'object' ? raw.props : {}
  const nodeType = String(props.node_type || '').toLowerCase()
  const entityType = String(props.entity_type || '').toLowerCase()
  if (nodeType === 'and' || nodeType === 'or') return `gate:${nodeType}`
  if (labels.includes('Chunk')) return 'chunk'
  if (labels.includes('Entity')) return entityType ? `entity:${entityType}` : 'entity'
  return labels.length ? `label:${labels[0]}` : 'node'
}

function colorForKind(kind) {
  // distinct, knowledge-graph-ish palette
  const map = {
    'gate:and': { bg: '#fffbeb', ring: '#f59e0b', fg: '#7c2d12' },
    'gate:or': { bg: '#faf5ff', ring: '#a855f7', fg: '#581c87' },
    chunk: { bg: '#ecfeff', ring: '#06b6d4', fg: '#0e7490' },
    entity: { bg: '#eff6ff', ring: '#3b82f6', fg: '#1d4ed8' },
  }
  if (map[kind]) return map[kind]
  if (kind.startsWith('entity:')) return { bg: '#eef2ff', ring: '#6366f1', fg: '#3730a3' }
  if (kind.startsWith('label:')) return { bg: '#f1f5f9', ring: '#64748b', fg: '#0f172a' }
  return { bg: '#f8fafc', ring: '#94a3b8', fg: '#0f172a' }
}

function KnowledgeGraphCircleNode({ data, selected }) {
  const raw = data?.raw || {}
  const name = raw?.props?.name || raw?.props?.normalized_name || raw?.graph_node_id
  const labels = Array.isArray(raw?.labels) ? raw.labels : []
  const kind = pickNodeKind(raw)
  const c = colorForKind(kind)
  const sub = labels.slice(0, 2).join(',')

  return (
    <div
      className={`kg-cnode${selected ? ' kg-cnode--selected' : ''}`}
      style={{
        background: c.bg,
        borderColor: c.ring,
        boxShadow: selected ? `0 0 0 4px rgba(99, 102, 241, 0.22), 0 16px 34px rgba(15, 23, 42, 0.18)` : undefined,
      }}
      title={String(name || '')}
    >
      {/* Invisible handles so edges can attach */}
      <Handle type="target" position={Position.Left} className="kg-handle" />
      <Handle type="source" position={Position.Right} className="kg-handle" />
      <div className="kg-cnode-name" style={{ color: c.fg }}>
        {String(name || '')}
      </div>
      {sub ? <div className="kg-cnode-sub">{sub}</div> : null}
    </div>
  )
}

function midpoint(a, b) {
  return (Number(a) + Number(b)) / 2
}

function ParallelGraphEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  markerEnd,
  label,
  style,
  data,
}) {
  const index = Number(data?.parallelIndex || 0)
  const count = Number(data?.parallelCount || 1)
  const pad = (count - 1) / 2
  const slot = index - pad
  const spacing = 18

  const dx = targetX - sourceX
  const dy = targetY - sourceY
  const len = Math.hypot(dx, dy) || 1
  const nx = -dy / len
  const ny = dx / len
  const offset = slot * spacing

  const sx = sourceX + nx * offset
  const sy = sourceY + ny * offset
  const tx = targetX + nx * offset
  const ty = targetY + ny * offset

  const path = `M ${sx},${sy} L ${tx},${ty}`

  const labelX = midpoint(sx, tx)
  const labelY = midpoint(sy, ty)

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
      {label ? (
        <EdgeLabelRenderer>
          <div
            className="kg-edge-label"
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
            }}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  )
}

function seededRand(seed) {
  let x = (seed % 2147483647) + 1
  return () => {
    x = (x * 48271) % 2147483647
    return x / 2147483647
  }
}

function layoutByConnectedComponents(nodes, edges) {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const adj = new Map(nodes.map((n) => [n.id, new Set()]))
  for (const e of edges) {
    const s = String(e.source)
    const t = String(e.target)
    if (!adj.has(s) || !adj.has(t)) continue
    adj.get(s).add(t)
    adj.get(t).add(s)
  }

  const comps = []
  const seen = new Set()
  for (const n of nodes) {
    if (seen.has(n.id)) continue
    const q = [n.id]
    seen.add(n.id)
    const members = []
    while (q.length) {
      const cur = q.pop()
      members.push(cur)
      for (const nb of adj.get(cur) || []) {
        if (seen.has(nb)) continue
        seen.add(nb)
        q.push(nb)
      }
    }
    comps.push(members)
  }

  // Force layout per component (simple FR-like)
  const positioned = []
  const packGapX = 620
  const packGapY = 520
  let packX = 0
  let packY = 0
  let rowH = 0

  const compSorted = comps.sort((a, b) => b.length - a.length)
  for (let ci = 0; ci < compSorted.length; ci++) {
    const memberIds = compSorted[ci]
    const size = memberIds.length
    const rand = seededRand(size * 997 + ci * 7919)
    const pos = new Map()
    const vel = new Map()

    const area = Math.max(1, size) * 220 * 220
    const k = Math.sqrt(area / Math.max(1, size))
    const iter = Math.min(240, 80 + size * 10)
    let temp = 120

    for (const id of memberIds) {
      pos.set(id, { x: (rand() - 0.5) * 320, y: (rand() - 0.5) * 240 })
      vel.set(id, { x: 0, y: 0 })
    }

    const compEdges = []
    const memberSet = new Set(memberIds)
    for (const e of edges) {
      const s = String(e.source)
      const t = String(e.target)
      if (memberSet.has(s) && memberSet.has(t)) compEdges.push([s, t])
    }

    for (let it = 0; it < iter; it++) {
      // repulsion
      for (let i = 0; i < memberIds.length; i++) {
        const a = memberIds[i]
        const pa = pos.get(a)
        let fx = 0
        let fy = 0
        for (let j = 0; j < memberIds.length; j++) {
          if (i === j) continue
          const b = memberIds[j]
          const pb = pos.get(b)
          const dx = pa.x - pb.x
          const dy = pa.y - pb.y
          const dist = Math.hypot(dx, dy) + 0.01
          const rep = (k * k) / dist
          fx += (dx / dist) * rep
          fy += (dy / dist) * rep
        }
        const v = vel.get(a)
        v.x += fx
        v.y += fy
      }
      // attraction along edges
      for (const [s, t] of compEdges) {
        const ps = pos.get(s)
        const pt = pos.get(t)
        const dx = ps.x - pt.x
        const dy = ps.y - pt.y
        const dist = Math.hypot(dx, dy) + 0.01
        const att = (dist * dist) / k
        const fx = (dx / dist) * att
        const fy = (dy / dist) * att
        const vs = vel.get(s)
        const vt = vel.get(t)
        vs.x -= fx
        vs.y -= fy
        vt.x += fx
        vt.y += fy
      }
      // apply / cool
      for (const id of memberIds) {
        const v = vel.get(id)
        const p = pos.get(id)
        const mag = Math.hypot(v.x, v.y) || 1
        const step = Math.min(mag, temp)
        p.x += (v.x / mag) * step
        p.y += (v.y / mag) * step
        v.x *= 0.35
        v.y *= 0.35
      }
      temp *= 0.92
    }

    // normalize bbox to start at (0,0)
    let minX = Infinity
    let minY = Infinity
    let maxX = -Infinity
    let maxY = -Infinity
    for (const id of memberIds) {
      const p = pos.get(id)
      minX = Math.min(minX, p.x)
      minY = Math.min(minY, p.y)
      maxX = Math.max(maxX, p.x)
      maxY = Math.max(maxY, p.y)
    }
    const w = Math.max(220, maxX - minX)
    const h = Math.max(220, maxY - minY)

    // pack components with spacing
    const maxRowW = 2400
    if (packX + w > maxRowW && packX > 0) {
      packX = 0
      packY += rowH + packGapY
      rowH = 0
    }
    rowH = Math.max(rowH, h)

    for (const id of memberIds) {
      const node = byId.get(id)
      const p = pos.get(id)
      positioned.push({
        ...node,
        position: {
          x: packX + (p.x - minX),
          y: packY + (p.y - minY),
        },
      })
    }
    packX += w + packGapX
  }

  return positioned
}

function asPrettyJson(value) {
  try {
    return JSON.stringify(value ?? null, null, 2)
  } catch {
    return String(value ?? '')
  }
}

export default function KnowledgeGraphModal({ open, onClose, initialCypher }) {
  const [cypher, setCypher] = useState(
    initialCypher ||
      "MATCH p=(a)-[r]->(b)\nRETURN p\nLIMIT 80",
  )
  const [neo4jDatabase, setNeo4jDatabase] = useState('neo4j')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [rawGraph, setRawGraph] = useState(null)
  const [selectedNode, setSelectedNode] = useState(null)
  const abortRef = useRef(null)
  const nodeTypes = useMemo(() => ({ circle: KnowledgeGraphCircleNode }), [])
  const edgeTypes = useMemo(() => ({ parallel: ParallelGraphEdge }), [])
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])

  const rfNodes = useMemo(() => {
    const items = rawGraph?.nodes || []
    const base = items.map((n) => {
      return {
        id: String(n.graph_node_id),
        type: 'circle',
        data: {
          raw: n,
        },
        position: { x: 0, y: 0 },
      }
    })
    // Layout using connected components so related nodes cluster and components keep distance.
    return layoutByConnectedComponents(base, rfEdgesForLayout(rawGraph))
  }, [rawGraph])

  function rfEdgesForLayout(graph) {
    const items = graph?.edges || []
    return items.map((e, idx) => ({
      id: String(e.graph_rel_id || `${e.source_graph_node_id}-${e.target_graph_node_id}-${idx}`),
      source: String(e.source_graph_node_id),
      target: String(e.target_graph_node_id),
    }))
  }

  const edgeColorFromLabel = useCallback((label) => {
    const key = String(label || '').trim()
    if (!key) return 'rgba(100, 116, 139, 0.62)'
    if (key.includes('触发')) return 'rgba(239, 68, 68, 0.70)'
    if (key.includes('包含') || key.includes('属于')) return 'rgba(59, 130, 246, 0.68)'
    if (key.includes('提及') || key.includes('mention')) return 'rgba(6, 182, 212, 0.68)'
    return 'rgba(99, 102, 241, 0.62)'
  }, [])

  const rfEdges = useMemo(() => {
    const items = rawGraph?.edges || []
    const keyOf = (e) => {
      const a = String(e.source_graph_node_id || '')
      const b = String(e.target_graph_node_id || '')
      // treat as undirected pair so A->B and B->A can still fan out
      return a < b ? `${a}__${b}` : `${b}__${a}`
    }
    const groups = new Map()
    for (const e of items) {
      const k = keyOf(e)
      if (!groups.has(k)) groups.set(k, [])
      groups.get(k).push(e)
    }

    const idxByEdge = new Map()
    for (const [k, group] of groups.entries()) {
      // stable ordering: by rel_id then by label
      const sorted = [...group].sort((a, b) => {
        const ra = String(a.graph_rel_id || '')
        const rb = String(b.graph_rel_id || '')
        if (ra && rb && ra !== rb) return ra < rb ? -1 : 1
        const la = String(a.rel_props?.relation_type || a.rel_type || '')
        const lb = String(b.rel_props?.relation_type || b.rel_type || '')
        if (la !== lb) return la < lb ? -1 : 1
        return 0
      })
      sorted.forEach((e, i) => idxByEdge.set(e, { i, count: sorted.length, k }))
    }

    return items.map((e, idx) => {
      const label = e.rel_props?.relation_type || e.rel_type || ''
      const stroke = edgeColorFromLabel(label)
      const meta = idxByEdge.get(e) || { i: 0, count: 1 }
      return {
        id: String(e.graph_rel_id || `${e.source_graph_node_id}-${e.target_graph_node_id}-${idx}`),
        source: String(e.source_graph_node_id),
        target: String(e.target_graph_node_id),
        type: meta.count > 1 ? 'parallel' : 'straight',
        label,
        animated: false,
        markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: stroke },
        style: { stroke, strokeWidth: 1.9 },
        data: {
          parallelIndex: meta.i,
          parallelCount: meta.count,
        },
      }
    })
  }, [rawGraph, edgeColorFromLabel])

  // When new graph arrives, initialize controlled nodes/edges.
  useEffect(() => {
    setNodes(rfNodes)
    setEdges(rfEdges)
  }, [rfNodes, rfEdges, setNodes, setEdges])

  const runQuery = useCallback(async () => {
    const q = String(cypher || '').trim()
    if (!q) return
    setError('')
    setLoading(true)
    setSelectedNode(null)
    if (abortRef.current) abortRef.current.abort()
    const ac = new AbortController()
    abortRef.current = ac
    try {
      const res = await graphCypherQuery({
        cypher: q,
        database: neo4jDatabase,
        limit: 300,
        signal: ac.signal,
      })
      setRawGraph(res?.graph || { nodes: [], edges: [] })
    } catch (e) {
      if (e?.name === 'AbortError') return
      setError(String(e?.message || e))
    } finally {
      setLoading(false)
    }
  }, [cypher, neo4jDatabase])

  useEffect(() => {
    if (!open) return undefined
    // open => auto load once
    void runQuery()
    return () => {
      if (abortRef.current) abortRef.current.abort()
      abortRef.current = null
    }
  }, [open, runQuery])

  if (!open) return null

  return (
    <div className="kg-overlay" role="presentation" onClick={onClose}>
      <div
        className="kg-modal"
        role="dialog"
        aria-modal="true"
        aria-label="知识图谱"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="kg-header">
          <div className="kg-header-main">
            <div className="kg-title">知识图谱</div>
            <div className="kg-subtitle">拖拽/缩放浏览；点击节点查看属性；输入 Cypher 查询子图。</div>
          </div>
          <button type="button" className="kg-close" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </header>

        <div className="kg-body">
          <section className="kg-left">
            <div className="kg-block kg-block--cypher">
              <div className="kg-block-title">Cypher 查询</div>
              <div className="kg-row">
                <label className="kg-label" htmlFor="kg-db">
                  Neo4j 数据库
                </label>
                <select
                  id="kg-db"
                  className="kg-select"
                  value={neo4jDatabase}
                  onChange={(e) => setNeo4jDatabase(e.target.value)}
                  disabled={loading}
                >
                  <option value="neo4j">neo4j</option>
                  <option value="system">system</option>
                </select>
              </div>
              <textarea
                className="kg-cypher"
                value={cypher}
                onChange={(e) => setCypher(e.target.value)}
                spellCheck={false}
              />
              <div className="kg-actions">
                <button type="button" className="kg-btn" onClick={runQuery} disabled={loading}>
                  {loading ? '查询中…' : '查询'}
                </button>
                <button
                  type="button"
                  className="kg-btn kg-btn--ghost"
                  onClick={() => setCypher("MATCH p=(a)-[r]->(b)\nRETURN p\nLIMIT 80")}
                  disabled={loading}
                >
                  示例
                </button>
              </div>
              {error ? <div className="kg-error">错误：{error}</div> : null}
              <div className="kg-hint">
                说明：后端只允许只读查询；建议显式加 <code>LIMIT</code>，避免拉取过大子图。
              </div>
            </div>

            <div className="kg-block kg-block--props">
              <div className="kg-block-title">节点属性</div>
              {selectedNode ? (
                <pre className="kg-json">{asPrettyJson(selectedNode)}</pre>
              ) : (
                <div className="kg-empty">请在图上点击一个节点。</div>
              )}
            </div>
          </section>

          <section className="kg-right">
            <div className="kg-canvas">
              <ReactFlow
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                edgeTypes={edgeTypes}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                fitView
                nodesDraggable
                nodesConnectable={false}
                elementsSelectable
                onNodeClick={(_, node) => setSelectedNode(node?.data?.raw || null)}
              >
                <MiniMap pannable zoomable />
                <Controls />
                <Background gap={20} size={1} color="rgba(148, 163, 184, 0.35)" />
              </ReactFlow>
            </div>
            <div className="kg-stats">
              <span>节点：{Number(rawGraph?.node_count ?? (rawGraph?.nodes || []).length) || 0}</span>
              <span>边：{Number(rawGraph?.edge_count ?? (rawGraph?.edges || []).length) || 0}</span>
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}

