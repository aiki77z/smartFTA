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

function normalizeEventTypeFromRaw(node) {
  const t = node?.type
  if (typeof t === 'string') {
    const key = t.trim()
    if (STRING_TYPE_LABEL_MAP[key]) return STRING_TYPE_LABEL_MAP[key]
    // 兼容旧格式：type 是 "1"/"2"/"3" 这样的数字字符串
    const asNum = Number(key)
    if (Number.isFinite(asNum)) {
      return TYPE_LABEL_MAP[asNum] || 'event'
    }
    const lower = key.toLowerCase()
    if (lower === 'top_event') return 'top'
    if (lower === 'intermediate_event') return 'intermediate'
    if (lower === 'basic_event') return 'basic'
    return 'event'
  }
  const typeCode = Number(t)
  return TYPE_LABEL_MAP[typeCode] || 'event'
}

function normalizeGateLabelFromRaw(node) {
  const g = node?.gate
  if (g === null || g === undefined || g === '') return ''
  if (typeof g === 'string') {
    const trimmed = g.trim()
    const u = trimmed.toUpperCase()
    if (u === 'AND' || u === 'OR') return u
    // 兼容旧格式：gate 是 "1"/"2" 这样的数字字符串
    const asNum = Number(trimmed)
    if (Number.isFinite(asNum)) {
      return GATE_LABEL_MAP[asNum] || ''
    }
    return ''
  }
  const gateCode = Number(g)
  return GATE_LABEL_MAP[gateCode] || ''
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
      const relation = parent.gate === 'AND' && e.meta?.relation?.member_relation
        ? e.meta.relation.member_relation
        : e.meta?.relation
      gateEdges.push({
        ...e,
        id: `${e.source}-${gateId}`,
        target: gateId,
        meta: {
          ...(e.meta || {}),
          relation,
          semanticSource: e.source,
          semanticTarget: targetId,
          visualGateSegment: 'input',
          raw: {
            ...(e.meta?.raw || {}),
            relation,
            sourceId: e.source,
            targetId,
          },
        },
      })
    })

    const gateRelation = (() => {
      if (parent.gate === 'AND') {
        const rel = incomingEdges.find((e) => e.meta?.relation?.gate_relation)?.meta?.relation?.gate_relation
        return rel || null
      }
      const relations = incomingEdges
        .map((e) => ({
          ...(e.meta?.relation || {}),
          semanticSource: e.source,
          semanticTarget: targetId,
          source_label: nodesById.get(e.source)?.label || e.source,
          target_label: parent.label || targetId,
        }))
        .filter(Boolean)
      if (relations.length === 1) return relations[0]
      return {
        relation_type: 'OR逻辑关系',
        gate_type: 'OR',
        relation_bundle: relations,
        documents: relations.flatMap((r) => Array.isArray(r.documents) ? r.documents : []),
        evidence_texts: relations.flatMap((r) => Array.isArray(r.evidence_texts) ? r.evidence_texts : []),
      }
    })()

    gateEdges.push({
      id: `${gateId}-${targetId}`,
      source: gateId,
      target: targetId,
      relation: parent.gate,
      meta: {
        gateFor: targetId,
        relation: gateRelation,
        semanticSource: parent.gate === 'OR' && incomingEdges.length === 1 ? incomingEdges[0].source : gateId,
        semanticTarget: targetId,
        visualGateSegment: 'output',
        raw: {
          relation: gateRelation,
          sourceId: parent.gate === 'OR' && incomingEdges.length === 1 ? incomingEdges[0].source : gateId,
          targetId,
        },
      },
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
    const type = normalizeEventTypeFromRaw(node)
    const gateLabel = normalizeGateLabelFromRaw(node)

    const x = typeof node.x === 'number' ? node.x : 0
    const y = typeof node.y === 'number' ? node.y : 0

    const baseLabel = node.name || (node.event && node.event.name) || node.id
    const label = baseLabel

    // gateCode：兼容 number / 数字字符串 / AND|OR 字符串
    const rawGate = node.gate
    const gateCode = (() => {
      if (typeof rawGate === 'number') return rawGate
      if (typeof rawGate === 'string') {
        const n = Number(rawGate.trim())
        if (Number.isFinite(n)) return n
      }
      if (gateLabel === 'AND') return 1
      if (gateLabel === 'OR') return 2
      return null
    })()

    const meta = {
      rawType: node.type,
      gateCode,
      gateLabel,
      event: node.event,
      transfer: node.transfer ?? '',
      raw: node,
    }
    if (node._ftaDiffStatus) {
      meta._diffStatus = node._ftaDiffStatus
      if (node._ftaDiffGhost) meta._diffGhost = true
    }
    return {
      id: String(node.id),
      label,
      type,
      position: { x, y },
      gate: gateLabel,
      meta,
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
      relation: link.relation?.relation_type || link.relation_type || '',
      meta: {
        isCondition: link.isCondition,
        relation: link.relation || null,
        raw: link,
      },
    }
  })

  return buildGraphWithGates(baseNodes, baseEdges)
}

/**
 * 将后端 GET /api/tree/{tree_id} 或版本文档转为画布用 graphData（与 FaultTreePage.normalizeToGraph 对齐）。
 * @param {object | null | undefined} raw
 * @returns {{ nodes: unknown[], edges: unknown[] }}
 */
export function normalizeBackendTreeDocumentToGraph(rawIn) {
  let raw = rawIn
  if (raw && typeof raw === 'object' && raw.treeData && !raw.tree_data) {
    raw = { ...raw, tree_data: raw.treeData }
  }
  if (!raw) return { nodes: [], edges: [] }
  if (
    raw.tree_data &&
    Array.isArray(raw.tree_data.nodeList) &&
    Array.isArray(raw.tree_data.linkList)
  ) {
    return parseRawFtaJson(raw.tree_data)
  }
  if (Array.isArray(raw.nodeList) && Array.isArray(raw.linkList)) {
    return parseRawFtaJson(raw)
  }
  if (raw.tree_data && raw.tree_data.nodes) {
    return parseTreeDataJson(raw)
  }
  if (Array.isArray(raw.nodes) && Array.isArray(raw.edges)) {
    return {
      nodes: raw.nodes.map((n) => ({
        id: String(n.id),
        label: n.label || n.name || n.title || String(n.id),
        type: n.type || 'event',
        level: n.level ?? n.depth ?? 0,
        meta: n.meta ?? n,
      })),
      edges: raw.edges.map((e, idx) => ({
        id: e.id ? String(e.id) : `e-${idx}`,
        source: String(e.source),
        target: String(e.target),
        relation: e.relation || e.gate || '',
        meta: e.meta ?? e,
      })),
    }
  }
  return { nodes: [], edges: [] }
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

    const meta = {
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
    }
    if (node._ftaDiffStatus) {
      meta._diffStatus = node._ftaDiffStatus
      if (node._ftaDiffGhost) meta._diffGhost = true
    }
    return {
      id: String(node.id),
      label: baseLabel,
      type: typeLabel,
      position: { x: 0, y: 0 },
      gate: gateLabel,
      meta,
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

/**
 * 将阈值列表压缩为自然语言中的一段（如 ± 对称区间只保留绝对值幅度）。
 * @param {unknown[]} thList
 * @param {string} symbol
 * @returns {string[]}
 */
function normalizeThresholdsForNaturalLanguage(thList, symbol) {
  if (!Array.isArray(thList) || thList.length === 0) return []
  const strs = thList
    .map((x) => (typeof x === 'object' && x !== null ? JSON.stringify(x) : String(x).trim()))
    .filter((s) => s !== '')
  if (strs.length === 0) return []
  if (strs.length === 1) return strs
  const sym = String(symbol || '').trim()
  if (strs.length === 2) {
    const a = Number(strs[0])
    const b = Number(strs[1])
    if (Number.isFinite(a) && Number.isFinite(b) && Math.abs(a) === Math.abs(b) && a !== 0) {
      if (['>', '>=', '<', '<='].includes(sym)) {
        return [String(Math.abs(a))]
      }
    }
  }
  if (strs.length === 2) return [`${strs[0]}、${strs[1]}`]
  return [strs.join('、')]
}

/**
 * 将单条触发规则转为近似自然语言（例：DISPLAY_MODULE表面静电电压 > 2000，瞬态）。
 * @param {unknown} rule
 * @returns {string}
 */
export function formatTriggerRuleNaturalLanguage(rule) {
  if (rule === null || rule === undefined) return '（空）'
  if (typeof rule !== 'object' || Array.isArray(rule)) return String(rule)

  const measure = String(rule.measurePointName ?? rule.measure_point_name ?? '').trim()
  const sym = String(rule.symbol ?? rule.operator ?? rule.op ?? '').trim()
  const rawTh = rule.thresholds ?? rule.threshold
  const duration = String(rule.duration ?? rule.time_window ?? rule.timeWindow ?? '').trim()
  const dev = String(rule.deviceTypeId ?? rule.device_type_id ?? '').trim()

  const thList = Array.isArray(rawTh)
    ? rawTh
    : rawTh !== undefined && rawTh !== null && rawTh !== ''
      ? [rawTh]
      : []
  const thSegments = normalizeThresholdsForNaturalLanguage(thList, sym)
  const thStr = thSegments.join('、')

  let core = ''
  if (measure && sym && thStr) {
    core = `${measure} ${sym} ${thStr}`
  } else if (measure && thStr && !sym) {
    core = `${measure} 为 ${thStr}`
  } else if (measure && sym && !thStr) {
    core = `${measure} ${sym}`
  } else if (measure) {
    core = measure
  } else if (thStr) {
    core = sym ? `${sym} ${thStr}` : thStr
  } else {
    try {
      return JSON.stringify(rule)
    } catch {
      return String(rule)
    }
  }

  const withDev = dev ? `[${dev}] ${core}` : core
  return duration ? `${withDev}，${duration}` : withDev
}

/**
 * 将规则数组格式化为多句自然语言（句间用中文分号分隔）。
 * @param {unknown} rules
 * @returns {string}
 */
export function formatTriggerRulesNaturalLanguage(rules) {
  if (!Array.isArray(rules)) return ''
  const lines = rules.map((r) => formatTriggerRuleNaturalLanguage(r)).filter(Boolean)
  return lines.join('；')
}

/**
 * 将单条「触发规则」对象转为详情面板可读的标签/值行（兼容 camelCase / snake_case）。
 * @param {unknown} rule
 * @returns {{ label: string, value: string }[]}
 */
export function getTriggerRuleDisplayRows(rule) {
  if (rule === null || rule === undefined) {
    return [{ label: '内容', value: '（空）' }]
  }
  if (typeof rule !== 'object' || Array.isArray(rule)) {
    return [{ label: '内容', value: String(rule) }]
  }

  const rows = []
  const consumed = new Set()

  const deviceType = rule.deviceTypeId ?? rule.device_type_id
  const measurePoint = rule.measurePointName ?? rule.measure_point_name
  const symbol = rule.symbol ?? rule.operator ?? rule.op
  const thresholds = rule.thresholds ?? rule.threshold
  const duration = rule.duration ?? rule.time_window ?? rule.timeWindow

  if (deviceType !== undefined && deviceType !== null && String(deviceType).trim() !== '') {
    rows.push({ label: '设备类型', value: String(deviceType) })
    consumed.add('deviceTypeId')
    consumed.add('device_type_id')
  }
  if (measurePoint !== undefined && measurePoint !== null && String(measurePoint).trim() !== '') {
    rows.push({ label: '测点', value: String(measurePoint) })
    consumed.add('measurePointName')
    consumed.add('measure_point_name')
  }
  if (symbol !== undefined && symbol !== null && String(symbol).trim() !== '') {
    rows.push({ label: '关系符', value: String(symbol) })
    consumed.add('symbol')
    consumed.add('operator')
    consumed.add('op')
  }
  if (thresholds !== undefined && thresholds !== null) {
    const t = Array.isArray(thresholds)
      ? thresholds
          .map((x) => (typeof x === 'object' && x !== null ? JSON.stringify(x) : String(x)))
          .join('、')
      : String(thresholds)
    if (t.trim()) {
      rows.push({ label: '阈值', value: t })
      consumed.add('thresholds')
      consumed.add('threshold')
    }
  }
  if (duration !== undefined && duration !== null && String(duration).trim() !== '') {
    rows.push({ label: '持续时间', value: String(duration) })
    consumed.add('duration')
    consumed.add('time_window')
    consumed.add('timeWindow')
  }

  for (const [k, v] of Object.entries(rule)) {
    if (consumed.has(k)) continue
    if (v === undefined || v === null || v === '') continue
    const val = typeof v === 'object' ? JSON.stringify(v) : String(v)
    if (!val.trim()) continue
    rows.push({ label: k, value: val })
  }

  if (!rows.length) {
    try {
      return [{ label: '内容', value: JSON.stringify(rule) }]
    } catch {
      return [{ label: '内容', value: String(rule) }]
    }
  }
  return rows
}

