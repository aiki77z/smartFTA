const GATE_LABEL_MAP = {
  1: 'AND',
  2: 'OR',
}

const TYPE_LABEL_MAP = {
  1: 'top',
  2: 'intermediate',
  3: 'basic',
}

const STRING_TYPE_LABEL_MAP = {
  top_event: 'top',
  intermediate_event: 'intermediate',
  basic_event: 'basic',
}

function buildGraphWithGates(baseNodes, baseEdges) {
  const nodesById = new Map(baseNodes.map((n) => [n.id, n]))
  const incomingByTarget = new Map()

  baseEdges.forEach((e) => {
    if (!incomingByTarget.has(e.target)) {
      incomingByTarget.set(e.target, [])
    }
    incomingByTarget.get(e.target).push(e)
  })

  const gateNodes = []
  const processedTargets = new Set()
  const gateEdges = []

  incomingByTarget.forEach((incomingEdges, targetId) => {
    const parent = nodesById.get(targetId)
    if (!parent || !parent.gate || incomingEdges.length === 0) return

    const gateId = `${targetId}-gate`
    if (processedTargets.has(targetId)) return
    processedTargets.add(targetId)

    const parentPos = parent.position || { x: 0, y: 0 }
    const childPositions = incomingEdges
      .map((e) => nodesById.get(e.source))
      .filter(Boolean)
      .map((n) => n.position || { x: 0, y: 0 })

    const avgChildX =
      childPositions.reduce((sum, p) => sum + p.x, 0) /
      (childPositions.length || 1)
    const avgChildY =
      childPositions.reduce((sum, p) => sum + p.y, 0) /
      (childPositions.length || 1)

    const gx = (parentPos.x + avgChildX) / 2
    const gy = (parentPos.y + avgChildY) / 2

    gateNodes.push({
      id: gateId,
      label: parent.gate,
      type: 'gate',
      position: { x: gx, y: gy },
      meta: {
        forNodeId: parent.id,
        gateLabel: parent.gate,
        rawParent: parent.meta?.raw,
      },
    })

    incomingEdges.forEach((e) => {
      gateEdges.push({
        ...e,
        id: `${e.source}-${gateId}`,
        target: gateId,
      })
    })

    gateEdges.push({
      id: `${gateId}-${targetId}`,
      source: gateId,
      target: targetId,
      relation: parent.gate,
      meta: { gateFor: targetId },
    })
  })

  const remainingEdges = baseEdges.filter(
    (e) => !processedTargets.has(e.target),
  )

  return {
    nodes: [...baseNodes, ...gateNodes],
    edges: [...remainingEdges, ...gateEdges],
  }
}

export function parseRawFtaJson(raw) {
  if (!raw || !Array.isArray(raw.nodeList) || !Array.isArray(raw.linkList)) {
    return { nodes: [], edges: [] }
  }

  const { nodeList, linkList } = raw

  const baseNodes = nodeList.map((node) => {
    const typeCode = Number(node.type)
    const gateCode = Number(node.gate)
    const gateLabel = GATE_LABEL_MAP[gateCode] || ''

    const x = typeof node.x === 'number' ? node.x : 0
    const y = typeof node.y === 'number' ? node.y : 0

    const baseLabel = node.name || (node.event && node.event.name) || node.id
    const label = baseLabel

    return {
      id: String(node.id),
      label,
      type: TYPE_LABEL_MAP[typeCode] || 'event',
      position: { x, y },
      gate: gateLabel,
      meta: {
        rawType: node.type,
        gateCode: node.gate,
        gateLabel,
        event: node.event,
        transfer: node.transfer,
        raw: node,
      },
    }
  })

  const baseEdges = linkList.map((link) => {
    const sourceId = String(link.sourceId)
    const targetId = String(link.targetId)
    const id = `${sourceId}-${targetId}`

    return {
      id,
      source: sourceId,
      target: targetId,
      relation: '',
      meta: {
        isCondition: link.isCondition,
        raw: link,
      },
    }
  })

  return buildGraphWithGates(baseNodes, baseEdges)
}

export function parseTreeDataJson(raw) {
  if (!raw || !raw.tree_data || !raw.tree_data.nodes) {
    return { nodes: [], edges: [] }
  }

  const treeData = raw.tree_data
  const nodeMap = treeData.nodes || {}
  const nodeList = Object.values(nodeMap)

  const baseNodes = nodeList.map((node) => {
    const typeLabel = STRING_TYPE_LABEL_MAP[node.type] || 'event'
    const gateRaw = typeof node.gate === 'string' ? node.gate.toUpperCase() : ''
    const gateLabel = gateRaw === 'AND' || gateRaw === 'OR' ? gateRaw : ''

    const baseLabel = node.name || node.id

    return {
      id: String(node.id),
      label: baseLabel,
      type: typeLabel,
      position: { x: 0, y: 0 },
      gate: gateLabel,
      meta: {
        rawType: node.type,
        gateCode: null,
        gateLabel,
        event: {
          id: node.id,
          name: node.name,
          description: node.description,
        },
        transfer: null,
        raw: node,
      },
    }
  })

  const baseEdges = []

  nodeList.forEach((node) => {
    if (!Array.isArray(node.children)) return
    const parentId = String(node.id)
    node.children.forEach((childId) => {
      const sourceId = String(childId)
      const targetId = parentId
      baseEdges.push({
        id: `${sourceId}-${targetId}`,
        source: sourceId,
        target: targetId,
        relation: '',
        meta: {
          raw: {
            parentId,
            childId: sourceId,
          },
        },
      })
    })
  })

  return buildGraphWithGates(baseNodes, baseEdges)
}

