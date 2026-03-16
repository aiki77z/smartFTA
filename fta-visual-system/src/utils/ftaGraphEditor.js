const TYPE_TO_CODE = { top: '1', intermediate: '2', basic: '3' }
const GATE_TO_CODE = { AND: '1', OR: '2' }
const STRING_TYPE_FROM_LABEL = {
  top: 'top_event',
  intermediate: 'intermediate_event',
  basic: 'basic_event',
}

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
      // 当 gate 不再有任何子节点时再清理掉该 gate；
      // 若仍有 1 个子节点，则保留 gate，避免用户删除一个子事件后逻辑门被自动移除。
      if (children.length === 0) {
        edges = edges.filter(
          (e) => e.source !== gate.id && e.target !== gate.id,
        )
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
    // 结构：子事件们 -> gateChildId -> parentId
    // 现在语义改为“在其下添加子节点”：在 parent 与 gate 之间插入新事件：
    // 子事件们 -> gateChildId -> newNode -> parentId

    // 强制新节点为中间事件，以避免“非叶基本事件”
    nodes = nodes.map((n) =>
      n.id === newId
        ? {
            ...n,
            type: 'intermediate',
            meta: { ...n.meta, rawType: TYPE_TO_CODE.intermediate },
          }
        : n,
    )

    // 修改 gate -> parent 的边为 gate -> newNode
    const gateParentEdge = edges.find(
      (e) => e.source === gateChildId && e.target === parentId,
    )
    if (gateParentEdge) {
      gateParentEdge.target = newId
    }

    // 新事件再指向原父节点
    edges.push({
      id: `${newId}-${parentId}`,
      source: newId,
      target: parentId,
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

export function addChildUnderGate(
  graphData,
  gateId,
  eventName,
  eventType = 'basic',
) {
  let nodes = [...graphData.nodes]
  let edges = [...graphData.edges]

  const gate = nodes.find((n) => n.id === gateId)
  if (!gate || gate.type !== 'gate') return graphData

  const newId = generateId()
  const newNode = {
    id: newId,
    label: eventName,
    type: eventType,
    position: { x: 0, y: 0 },
    gate: '',
    meta: {
      rawType: TYPE_TO_CODE[eventType] || TYPE_TO_CODE.basic,
      gateCode: '',
      gateLabel: '',
      event: makeDefaultEvent(eventName),
      transfer: '',
    },
  }
  nodes.push(newNode)

  edges.push({
    id: `${newId}-${gateId}`,
    source: newId,
    target: gateId,
    relation: '',
    meta: {},
  })

  return { nodes, edges }
}

export function deleteNode(graphData, nodeId) {
  let nodes = [...graphData.nodes]
  let edges = [...graphData.edges]

  const node = nodes.find((n) => n.id === nodeId)
  if (!node) return graphData

  // 删除顶事件：
  // 仅当图中还存在“其他顶事件节点”时才允许删除当前顶事件，
  // 否则直接忽略（避免把唯一的顶事件删掉）。
  if (node.type === 'top') {
    const otherTopExists = nodes.some(
      (n) => n.id !== nodeId && n.type === 'top',
    )
    if (!otherTopExists) return graphData

    // 只移除该顶事件本身以及指向它的边，
    // 保留其下方的逻辑门和事件子节点，使之成为悬空子树（由校验服务报错）
    nodes = nodes.filter((n) => n.id !== nodeId)
    edges = edges.filter((e) => e.target !== nodeId && e.source !== nodeId)
    return autoCleanGates({ nodes, edges })
  }

  // 如果是中间事件：将其子事件“提升”到父节点，保持原有故障路径
  if (node.type === 'intermediate') {
    const parentId = getParent(edges, nodeId)
    if (parentId) {
      // 找到挂在该中间事件下面的逻辑门（如果有）
      const gateChildId = getGateChild(nodes, edges, nodeId)

      if (gateChildId) {
        // 结构：children -> gateChildId -> nodeId -> parentId
        // 期望：children -> gateChildId -> parentId

        // 1）将 gate -> node 的边改为 gate -> parent
        edges = edges.map((e) =>
          e.source === gateChildId && e.target === nodeId
            ? { ...e, target: parentId }
            : e,
        )

        // 2）删除与该中间事件相关的其他边
        edges = edges.filter((e) => e.source !== nodeId && e.target !== nodeId)

        // 3）删除该中间事件节点本身，但保留 gate 节点和其与子事件的连接
        nodes = nodes.filter((n) => n.id !== nodeId)

        return autoCleanGates({ nodes, edges })
      } else {
        // 无 gate：直接将子事件挂到父节点
        const childEventIds = getChildren(edges, nodeId).filter((cid) => {
          const cn = nodes.find((n) => n.id === cid)
          return cn && cn.type !== 'gate'
        })

        edges = edges.filter((e) => e.source !== nodeId && e.target !== nodeId)
        nodes = nodes.filter((n) => n.id !== nodeId)

        for (const childId of childEventIds) {
          edges.push({
            id: `${childId}-${parentId}`,
            source: childId,
            target: parentId,
            relation: '',
            meta: {},
          })
        }

        return autoCleanGates({ nodes, edges })
      }
    }
    // 如果没有父节点（例如异常结构），则退回到原有“整棵删除”逻辑
  }

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

export function changeEventDescription(graphData, nodeId, newDescription) {
  const nodes = graphData.nodes.map((n) => {
    if (n.id !== nodeId) return n
    if (!n.meta || !n.meta.event) return n
    return {
      ...n,
      meta: {
        ...n.meta,
        event: {
          ...n.meta.event,
          description: newDescription,
        },
      },
    }
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
  // 没有直接事件子节点时无法插入逻辑门
  if (directChildren.length === 0) return graphData

  const gateId = generateId() + '-gate'
  nodes.push({
    id: gateId,
    label: gateType,
    type: 'gate',
    position: { x: 0, y: 0 },
    meta: { forNodeId: parentId, gateLabel: gateType },
  })

  // 删除 parent 与这些直接子节点之间的原始边
  edges = edges.filter(
    (e) => !(e.target === parentId && directChildren.includes(e.source)),
  )

  // 将这些事件子节点连到 gate
  for (const childId of directChildren) {
    edges.push({
      id: `${childId}-${gateId}`,
      source: childId,
      target: gateId,
      relation: '',
      meta: {},
    })
  }

  // gate 再连回 parent
  edges.push({
    id: `${gateId}-${parentId}`,
    source: gateId,
    target: parentId,
    relation: gateType,
    meta: { gateFor: parentId },
  })

  return { nodes, edges }
}

export function duplicateEventNode(graphData, nodeId) {
  let nodes = [...graphData.nodes]
  let edges = [...graphData.edges]

  const node = nodes.find((n) => n.id === nodeId)
  if (!node || node.type === 'gate') return graphData

  const newId = generateId()
  const baseLabel = node.label || '事件'
  const copyLabel = `${baseLabel}(副本)`

  const newNode = {
    ...node,
    id: newId,
    label: copyLabel,
    position: { x: (node.position?.x || 0) + 40, y: (node.position?.y || 0) + 40 },
    meta: node.meta
      ? {
          ...node.meta,
          event: node.meta.event ? { ...node.meta.event, id: newId, name: copyLabel } : undefined,
          raw: undefined,
        }
      : undefined,
  }
  nodes.push(newNode)

  // 复制节点时，仅保留名称和详细信息，不复制任何父子关系或逻辑门结构
  return { nodes, edges }
}

export function connectNodes(graphData, { sourceId, targetId }) {
  let nodes = [...graphData.nodes]
  let edges = [...graphData.edges]

  const sourceNode = nodes.find((n) => n.id === sourceId)
  const targetNode = nodes.find((n) => n.id === targetId)
  if (!sourceNode || !targetNode) return graphData
  // 不允许把逻辑门作为“子节点”，但允许把事件挂到逻辑门下面
  if (sourceNode.type === 'gate') return graphData

  const childId = sourceId
  const parentId = targetId

  // 避免自环
  if (childId === parentId) return graphData

  // 避免重复边
  if (edges.some((e) => e.source === childId && e.target === parentId)) {
    return graphData
  }

  // 目标是逻辑门：直接挂到该门下面
  if (targetNode.type === 'gate') {
    edges.push({
      id: `${childId}-${parentId}`,
      source: childId,
      target: parentId,
      relation: '',
      meta: {},
    })
    return { nodes, edges }
  }

  // 若父是 basic，则先升级为 intermediate
  if (targetNode.type === 'basic') {
    nodes = nodes.map((n) =>
      n.id === parentId
        ? { ...n, type: 'intermediate', meta: { ...n.meta, rawType: TYPE_TO_CODE.intermediate } }
        : n,
    )
  }

  const gateChildId = getGateChild(nodes, edges, parentId)
  if (gateChildId) {
    // 父下已有 gate：直接把 child 挂到 gate 下
    edges.push({
      id: `${childId}-${gateChildId}`,
      source: childId,
      target: gateChildId,
      relation: '',
      meta: {},
    })
    return { nodes, edges }
  }

  const currentChildren = getChildren(edges, parentId)
  if (currentChildren.length === 0) {
    // 父目前没有子节点，直接挂上去
    edges.push({
      id: `${childId}-${parentId}`,
      source: childId,
      target: parentId,
      relation: '',
      meta: {},
    })
    return { nodes, edges }
  }

  // 父已有子节点但还没有 gate：需要插入 OR 门，再把所有子节点挂到门下
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

  // 新连接的 child 也挂到 gate 下
  edges.push({
    id: `${childId}-${gateId}`,
    source: childId,
    target: gateId,
    relation: '',
    meta: {},
  })

  edges.push({
    id: `${gateId}-${parentId}`,
    source: gateId,
    target: parentId,
    relation: 'OR',
    meta: { gateFor: parentId },
  })

  return { nodes, edges }
}

export function deleteEdgeById(graphData, edgeId) {
  const nodes = [...graphData.nodes]
  const edges = graphData.edges.filter((e) => e.id !== edgeId)
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

export function graphToTreeDataJson(graphData, original) {
  const { nodes, edges } = graphData
  const eventNodes = nodes.filter((n) => n.type !== 'gate')

  const nodesById = new Map(nodes.map((n) => [n.id, n]))

  const gateForEvent = new Map()
  const gateByParent = new Map()
  const gateNodes = nodes.filter((n) => n.type === 'gate')
  for (const gate of gateNodes) {
    const parentEdge = edges.find((e) => e.source === gate.id)
    if (parentEdge) {
      gateForEvent.set(parentEdge.target, gate.label)
      gateByParent.set(parentEdge.target, gate)
    }
  }

  const originalTreeData = original?.tree_data || {}
  const originalNodes = originalTreeData.nodes || {}
  const newNodes = {}

  eventNodes.forEach((n) => {
    const prev = originalNodes[n.id] || {}
    const gateLabel = gateForEvent.has(n.id) ? gateForEvent.get(n.id) : null
     let children = []

    const gateNode = gateByParent.get(n.id)
    if (gateNode) {
      // 通过逻辑门连接的子节点：gate 的事件子节点
      children = edges
        .filter((e) => e.target === gateNode.id)
        .map((e) => nodesById.get(e.source))
        .filter((cn) => cn && cn.type !== 'gate')
        .map((cn) => cn.id)
    } else {
      // 直接连接的事件子节点
      children = edges
        .filter((e) => e.target === n.id)
        .map((e) => nodesById.get(e.source))
        .filter((cn) => cn && cn.type !== 'gate')
        .map((cn) => cn.id)
    }
    newNodes[n.id] = {
      ...prev,
      id: n.id,
      name: n.label,
      type: STRING_TYPE_FROM_LABEL[n.type] || prev.type || 'basic_event',
      gate: gateLabel,
      description: n.meta?.event?.description ?? prev.description,
      children,
    }
  })

  return {
    ...(original || {}),
    tree_data: {
      ...originalTreeData,
      nodes: newNodes,
    },
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
  const hasDirectChildren = directEventChildren.length >= 1
  const isTop = node.type === 'top'
  const childCount = getChildren(edges, nodeId).length
  const parentId = getParent(edges, nodeId)

  return {
    node,
    isGate,
    gateChild,
    hasDirectChildren,
    isTop,
    childCount,
    parentId,
  }
}

export function insertParentEvent(
  graphData,
  childId,
  eventName = '新中间事件',
  eventType = 'intermediate',
) {
  let nodes = [...graphData.nodes]
  let edges = [...graphData.edges]

  const childNode = nodes.find((n) => n.id === childId)
  if (!childNode) return graphData

  const directParentId = getParent(edges, childId)
  if (!directParentId) return graphData

  const directParentNode = nodes.find((n) => n.id === directParentId)
  if (!directParentNode) return graphData

  const newId = generateId()
  const newNode = {
    id: newId,
    label: eventName,
    type: eventType,
    position: { x: 0, y: 0 },
    gate: '',
    meta: {
      rawType: TYPE_TO_CODE[eventType] || TYPE_TO_CODE.intermediate,
      gateCode: '',
      gateLabel: '',
      event: makeDefaultEvent(eventName),
      transfer: '',
    },
  }
  nodes.push(newNode)

  if (directParentNode.type === 'gate') {
    // 结构为：child -> gate -> (event...)
    // 在 gate 与 child 之间插入新事件：child -> newId -> gate
    edges = edges.map((e) =>
      e.source === childId && e.target === directParentId
        ? { ...e, target: newId }
        : e,
    )

    edges.push({
      id: `${newId}-${directParentId}`,
      source: newId,
      target: directParentId,
      relation: '',
      meta: {},
    })
  } else {
    // 结构为：child -> eventParentId（无 gate）
    // 改为：child -> newId -> eventParentId
    edges = edges.map((e) =>
      e.source === childId && e.target === directParentId
        ? { ...e, target: newId }
        : e,
    )

    edges.push({
      id: `${newId}-${directParentId}`,
      source: newId,
      target: directParentId,
      relation: '',
      meta: {},
    })
  }

  return { nodes, edges }
}
