const DEFAULT_BASE_URL = 'http://localhost:8000'

function getBaseUrl() {
  const envUrl = import.meta?.env?.VITE_FTA_BACKEND_URL
  return (envUrl || DEFAULT_BASE_URL).replace(/\/+$/, '')
}

async function requestJson(path, { method = 'GET', body, signal } = {}) {
  const baseUrl = getBaseUrl()
  const url = `${baseUrl}${path.startsWith('/') ? path : `/${path}`}`

  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })

  if (res.ok) return await res.json()

  let detail = ''
  try {
    const data = await res.json()
    detail = data?.detail ? JSON.stringify(data.detail) : JSON.stringify(data)
  } catch {
    try {
      detail = await res.text()
    } catch {
      detail = ''
    }
  }
  const err = new Error(`后端接口错误（${res.status}）：${detail || res.statusText}`)
  err.status = res.status
  err.detail = detail
  throw err
}

export function generateTree({ prompt, signal } = {}) {
  return requestJson('/api/tree/generate', { method: 'POST', body: { prompt }, signal })
}

export function getTree({ treeId, signal } = {}) {
  return requestJson(`/api/tree/${encodeURIComponent(treeId)}`, { signal })
}

export function getTreeVersion({ treeId, version, signal } = {}) {
  return requestJson(
    `/api/tree/${encodeURIComponent(treeId)}/version/${encodeURIComponent(version)}`,
    { signal },
  )
}

export function getTreeHistory({ treeId, signal } = {}) {
  return requestJson(`/api/tree/${encodeURIComponent(treeId)}/history`, { signal })
}

export function rollbackTree({ treeId, targetVersion, signal } = {}) {
  return requestJson(
    `/api/tree/${encodeURIComponent(treeId)}/rollback/${encodeURIComponent(targetVersion)}`,
    { method: 'POST', signal },
  )
}

export function saveTree({ treeId, treeData, editor = '专家', description = '手动修改', signal } = {}) {
  return requestJson(`/api/tree/${encodeURIComponent(treeId)}/save`, {
    method: 'POST',
    body: { tree_data: treeData, editor, description },
    signal,
  })
}

export function validateTree({ treeData, signal } = {}) {
  return requestJson('/api/tree/validate', { method: 'POST', body: { tree_data: treeData }, signal })
}

export function validateTreeSemantic({ treeData, signal } = {}) {
  return requestJson('/api/tree/validate/semantic', {
    method: 'POST',
    body: { tree_data: treeData },
    signal,
  })
}

// Legacy validator-service endpoints (now merged into the backend main:app)
export function validateFaultTreeGraph({ graph, signal } = {}) {
  return requestJson('/validate-fault-tree', { method: 'POST', body: { graph }, signal })
}

export function getChunk({ chunkId, signal } = {}) {
  return requestJson(`/api/chunk/${encodeURIComponent(chunkId)}`, { signal })
}

export function getCorrections({ treeId, signal } = {}) {
  return requestJson(`/api/corrections/${encodeURIComponent(treeId)}`, { signal })
}

