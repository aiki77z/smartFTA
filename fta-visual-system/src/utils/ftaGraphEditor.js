const TYPE_TO_CODE = { top: '1', intermediate: '2', basic: '3' }
const GATE_TO_CODE = { AND: '1', OR: '2' }

function generateId() {
  return `node-${Date.now().toString(36)}${Math.random().toString(36).substr(2, 9)}`
}

function getChildren(edges, nodeId) {
  return edges.filter((e) => e.target === nodeId).map((e) => e.source)
}

function getParent(edges, nodeId) {
  const edge = edges.find((e) => e.source === nodeId)
  return edge ? edge.target : null
}

function getGateChild(nodes, edges, eventId) {
  const childIds = getChildren(edges, eventId)
  return childIds.find((id) => {
    const node = nodes.find((n) => n.id === id)
    return node && node.type === 'gate'
  })
}

function getDirectEventChildren(nodes, edges, parentId) {
  return getChildren(edges, parentId).filter((id) => {
    const node = nodes.find((n) => n.id === id)
    return node && node.type !== 'gate'
  })
}

function autoCleanGates(graphData) {
  let { nodes, edges } = graphData
  let changed = true
  while (changed) {
    changed = false
    const gateNodes = nodes.filter((n) => n.type === 'gate')
    for (const gate of gateNodes) {
      const children = getChildren(edges, gate.id)
      const parentId = getParent(edges, gate.id)
      if (children.length <= 1) {
        if (children.length === 1 && parentId) {
          edges = edges.filter(
            (e) => e.source !== gate.id && e.target !== gate.id,
          )
          edges.push({
            id: `${children[0]}-${parentId}`,
            source: children[0],
            target: parentId,
            relation: '',
            meta: {},
          })
        } else {
          edges = edges.filter(
            (e) => e.source !== gate.id && e.target !== gate.id,
          )
        }
        nodes = nodes.filter((n) => n.id !== gate.id)
        changed = true
        break
      }
    }
  }
  return { nodes, edges }
}

function makeDefaultEvent(name) {
  return {
    id: '',
    name,
    description: '',
    errorLevel: '',
    priority: 0,
    probability: 1e-8,
    showProbability: 0.000001,
    rules: [],
    investigateMethod: '',
    documents: [],
  }
}

export function addChildNode(
  graphData,
  parentId,
  eventName,
  eventType = 'basic',
) {
  let nodes = [...graphData.nodes]
  let edges = [...graphData.edges]

  const parentNode = nodes.find((n) => n.id === parentId)
  if (parentNode && parentNode.type === 'basic') {
    nodes = nodes.map((n) =>
      n.id === parentId
        ? { ...n, type: 'intermediate', meta: { ...n.meta, rawType: '2' } }
        : n,
    )
  }

  const newId = generateId()
  const newNode = {
    id: newId,
    label: eventName,
    type: eventType,
    position: { x: 0, y: 0 },
    gate: '',
    meta: {
      rawType: TYPE_TO_CODE[eventType],
      gateCode: '',
      gateLabel: '',
      event: makeDefaultEvent(eventName),
      transfer: '',
    },
  }
  nodes.push(newNode)

  const gateChildId = getGateChild(nodes, edges, parentId)

  if (gateChildId) {
    edges.push({
      id: `${newId}-${gateChildId}`,
      source: newId,
      target: gateChildId,
      relation: '',
      meta: {},
    })
  } else {
    const currentChildren = getChildren(edges, parentId)
    if (currentChildren.length === 0) {
      edges.push({
        id: `${newId}-${parentId}`,
        source: newId,
        target: parentId,
        relation: '',
        meta: {},
      })
    } else {
      const gateId = generateId() + '-gate'
      const gateNode = {
        id: gateId,
        label: 'OR',
        type: 'gate',
        position: { x: 0, y: 0 },
        meta: { forNodeId: parentId, gateLabel: 'OR' },
      }
      nodes.push(gateNode)

      const childEdges = edges.filter((e) => e.target === parentId)
      edges = edges.filter((e) => e.target !== parentId)

      for (const ce of childEdges) {
        edges.push({
          id: `${ce.source}-${gateId}`,
          source: ce.source,
          target: gateId,
          relation: '',
          meta: {},
        })
      }

      edges.push({
        id: `${gateId}-${parentId}`,
        source: gateId,
        target: parentId,
        relation: 'OR',
        meta: { gateFor: parentId },
      })

      edges.push({
        id: `${newId}-${gateId}`,
        source: newId,
        target: gateId,
        relation: '',
        meta: {},
      })
    }
  }

  return autoCleanGates({ nodes, edges })
}

export function deleteNode(graphData, nodeId) {
  let nodes = [...graphData.nodes]
  let edges = [...graphData.edges]

  const node = nodes.find((n) => n.id === nodeId)
  if (!node) return graphData
  if (node.type === 'top') return graphData

  function getDescendants(nId) {
    const visited = new Set()
    const queue = [nId]
    while (queue.length) {
      const current = queue.shift()
      if (visited.has(current)) continue
      visited.add(current)
      const children = getChildren(edges, current)
      queue.push(...children)
    }
    visited.delete(nId)
    return visited
  }

  const descendants = getDescendants(nodeId)
  const toRemove = new Set([nodeId, ...descendants])

  nodes = nodes.filter((n) => !toRemove.has(n.id))
  edges = edges.filter(
    (e) => !toRemove.has(e.source) && !toRemove.has(e.target),
  )

  return autoCleanGates({ nodes, edges })
}

export function deleteGate(graphData, gateId) {
  let nodes = [...graphData.nodes]
  let edges = [...graphData.edges]

  const gate = nodes.find((n) => n.id === gateId)
  if (!gate || gate.type !== 'gate') return graphData

  const parentId = getParent(edges, gateId)
  const childIds = getChildren(edges, gateId)

  nodes = nodes.filter((n) => n.id !== gateId)
  edges = edges.filter((e) => e.source !== gateId && e.target !== gateId)

  if (parentId) {
    for (const childId of childIds) {
      edges.push({
        id: `${childId}-${parentId}`,
        source: childId,
        target: parentId,
        relation: '',
        meta: {},
      })
    }
  }

  return { nodes, edges }
}

export function changeGateType(graphData, gateId, newType) {
  const nodes = graphData.nodes.map((n) => {
    if (n.id === gateId && n.type === 'gate') {
      return { ...n, label: newType, meta: { ...n.meta, gateLabel: newType } }
    }
    return n
  })
  return { ...graphData, nodes, edges: graphData.edges }
}

export function renameNode(graphData, nodeId, newName) {
  const nodes = graphData.nodes.map((n) => {
    if (n.id === nodeId) {
      const updated = { ...n, label: newName }
      if (n.meta?.event) {
        updated.meta = {
          ...n.meta,
          event: { ...n.meta.event, name: newName },
        }
      }
      return updated
    }
    return n
  })
  return { ...graphData, nodes, edges: graphData.edges }
}

export function changeEventType(graphData, nodeId, newType) {
  const nodes = graphData.nodes.map((n) => {
    if (n.id === nodeId && n.type !== 'gate') {
      return { ...n, type: newType, meta: { ...n.meta, rawType: TYPE_TO_CODE[newType] } }
    }
    return n
  })
  return { ...graphData, nodes, edges: graphData.edges }
}

export function insertGate(graphData, parentId, gateType = 'OR') {
  let nodes = [...graphData.nodes]
  let edges = [...graphData.edges]

  const directChildren = edges
    .filter((e) => e.target === parentId)
    .map((e) => e.source)
    .filter((id) => {
      const node = nodes.find((n) => n.id === id)
      return node && node.type !== 'gate'
    })

  if (directChildren.length < 2) return graphData

  const gateId = generateId() + '-gate'
  nodes.push({
    id: gateId,
    label: gateType,
    type: 'gate',
    position: { x: 0, y: 0 },
    meta: { forNodeId: parentId, gateLabel: gateType },
  })

  edges = edges.filter(
    (e) => !(e.target === parentId && directChildren.includes(e.source)),
  )

  for (const childId of directChildren) {
    edges.push({
      id: `${childId}-${gateId}`,
      source: childId,
      target: gateId,
      relation: '',
      meta: {},
    })
  }

  edges.push({
    id: `${gateId}-${parentId}`,
    source: gateId,
    target: parentId,
    relation: gateType,
    meta: { gateFor: parentId },
  })

  return { nodes, edges }
}

export function graphToRawJson(graphData, attr) {
  const { nodes, edges } = graphData
  const eventNodes = nodes.filter((n) => n.type !== 'gate')
  const gateNodes = nodes.filter((n) => n.type === 'gate')

  const gateForEvent = new Map()
  for (const gate of gateNodes) {
    const parentEdge = edges.find((e) => e.source === gate.id)
    if (parentEdge) {
      gateForEvent.set(parentEdge.target, gate)
    }
  }

  const nodeList = eventNodes.map((n) => {
    const gate = gateForEvent.get(n.id)
    const gateCode = gate
      ? GATE_TO_CODE[gate.label] || '2'
      : n.meta?.gateCode || '2'

    return {
      type: TYPE_TO_CODE[n.type] || '3',
      gate: gateCode,
      name: n.label.replace(/^\[(AND|OR)\]\s*/, ''),
      event: n.meta?.event || null,
      x: Math.round(n.position?.x || 0),
      y: Math.round(n.position?.y || 0),
      id: n.id,
      transfer: n.meta?.transfer || '',
    }
  })

  const linkList = []

  for (const gate of gateNodes) {
    const parentEdge = edges.find((e) => e.source === gate.id)
    if (!parentEdge) continue
    const parentId = parentEdge.target
    const childIds = edges
      .filter((e) => e.target === gate.id)
      .map((e) => e.source)
    for (const childId of childIds) {
      if (eventNodes.some((n) => n.id === childId)) {
        linkList.push({
          type: 'link',
          sourceId: childId,
          targetId: parentId,
          isCondition: false,
        })
      }
    }
  }

  for (const e of edges) {
    const sourceNode = nodes.find((n) => n.id === e.source)
    const targetNode = nodes.find((n) => n.id === e.target)
    if (sourceNode?.type !== 'gate' && targetNode?.type !== 'gate') {
      linkList.push({
        type: 'link',
        sourceId: e.source,
        targetId: e.target,
        isCondition: false,
      })
    }
  }

  return {
    attr: attr || {
      background: '#fff',
      fontColor: '#000',
      eventColor: '#000',
      eventFillColor: '#fff',
      gateColor: '#000',
      gateFillColor: '#fff',
      linkColor: '#456',
      eventCode: true,
      eventProbability: false,
      containerX: 0,
      containerY: 0,
      width: 1920,
      height: 1080,
    },
    nodeList,
    linkList,
  }
}

export function getNodeEditInfo(graphData, nodeId) {
  const { nodes, edges } = graphData
  const node = nodes.find((n) => n.id === nodeId)
  if (!node) return null

  const isGate = node.type === 'gate'
  const gateChildId = isGate ? null : getGateChild(nodes, edges, nodeId)
  const gateChild = gateChildId
    ? nodes.find((n) => n.id === gateChildId)
    : null
  const directEventChildren = isGate
    ? []
    : getDirectEventChildren(nodes, edges, nodeId)
  const hasDirectChildren = directEventChildren.length >= 2
  const isTop = node.type === 'top'
  const childCount = getChildren(edges, nodeId).length

  return {
    node,
    isGate,
    gateChild,
    hasDirectChildren,
    isTop,
    childCount,
  }
}
