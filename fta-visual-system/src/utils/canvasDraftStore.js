const PREFIX = 'fta-canvas-drafts:'

function safeParse(raw, fallback) {
  try {
    return JSON.parse(raw)
  } catch {
    return fallback
  }
}

export function readCanvasDraftMap(projectId) {
  if (typeof window === 'undefined' || !projectId) return {}
  try {
    const raw = localStorage.getItem(`${PREFIX}${projectId}`)
    if (!raw) return {}
    const o = safeParse(raw, null)
    return o && typeof o === 'object' ? o : {}
  } catch {
    return {}
  }
}

function writeCanvasDraftMap(projectId, map) {
  if (typeof window === 'undefined' || !projectId) return
  try {
    localStorage.setItem(`${PREFIX}${projectId}`, JSON.stringify(map || {}))
  } catch {
    // quota
  }
}

export function upsertCanvasDraft(projectId, canvasId, draft) {
  if (!projectId || !canvasId) return
  const map = readCanvasDraftMap(projectId)
  map[canvasId] = {
    ...draft,
    canvasId,
    projectId,
    updatedAt: Date.now(),
  }
  // cap ~40 drafts per project
  const entries = Object.entries(map).sort((a, b) => Number(b[1]?.updatedAt || 0) - Number(a[1]?.updatedAt || 0))
  const trimmed = Object.fromEntries(entries.slice(0, 40))
  writeCanvasDraftMap(projectId, trimmed)
}

export function removeCanvasDraft(projectId, canvasId) {
  if (!projectId || !canvasId) return
  const map = readCanvasDraftMap(projectId)
  if (!map[canvasId]) return
  delete map[canvasId]
  writeCanvasDraftMap(projectId, map)
}

export function listCanvasDrafts(projectId) {
  const map = readCanvasDraftMap(projectId)
  return Object.values(map).sort((a, b) => Number(b?.updatedAt || 0) - Number(a?.updatedAt || 0))
}

/** 用于首页展示：从画布 JSON 文本推断顶事件名称 */
export function inferCanvasDisplayTitle(rawJsonText, graphData) {
  try {
    if (graphData?.nodes?.length) {
      const top = graphData.nodes.find((n) => n?.type === 'top' || n?.data?.type === 'top')
      const label = top?.data?.label ?? top?.label
      if (typeof label === 'string' && label.trim()) return label.trim()
    }
    const parsed = typeof rawJsonText === 'string' ? JSON.parse(rawJsonText) : rawJsonText
    if (parsed?.tree_data?.nodeList) {
      const nl = parsed.tree_data.nodeList
      const topNode = nl.find((n) => n?.type === 'TOP' || n?.type === 'Top' || n?.nodeType === 'TOP')
      if (topNode?.name) return String(topNode.name).trim()
    }
    if (parsed?.nodes && Array.isArray(parsed.nodes)) {
      const top = parsed.nodes.find((n) => n?.type === 'top')
      if (top?.label) return String(top.label).trim()
    }
  } catch {
    /* ignore */
  }
  return '空白画布'
}
