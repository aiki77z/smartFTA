const DEFAULT_BASE_URL = 'http://localhost:8000'

function getBaseUrl() {
  const envUrl = import.meta?.env?.VITE_FTA_BACKEND_URL
  return (envUrl || DEFAULT_BASE_URL).replace(/\/+$/, '')
}

async function requestJson(path, { method = 'GET', body, signal } = {}) {
  const baseUrl = getBaseUrl()
  const url = `${baseUrl}${path.startsWith('/') ? path : `/${path}`}`

  let res
  try {
    res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (e) {
    const msg = String(e?.message || '').toLowerCase()
    // 统一把“被取消的请求”归一成 AbortError，页面侧可直接忽略，不展示给用户
    if (e?.name === 'AbortError' || msg.includes('aborted') || msg.includes('signal is aborted')) {
      const err = new Error('Request aborted')
      err.name = 'AbortError'
      throw err
    }
    throw e
  }

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

/** POST /api/batch/generate-all — 批量生成当前知识库可识别的全部顶事件故障树 */
export function generateAllTrees({ signal } = {}) {
  return requestJson('/api/batch/generate-all', { method: 'POST', signal })
}

/** GET /api/batch/job/{job_id} — 批任务整体进度 */
export function getBatchJob({ jobId, signal } = {}) {
  return requestJson(`/api/batch/job/${encodeURIComponent(jobId)}`, { signal })
}

/** GET /api/batch/job-item/{item_id} — 单任务项进度（与异步生成配合） */
export function getBatchJobItem({ itemId, signal } = {}) {
  return requestJson(`/api/batch/job-item/${encodeURIComponent(itemId)}`, { signal })
}

/**
 * 轮询任务项直到 success / failed。
 * @param {object} opts
 * @param {string} opts.itemId
 * @param {AbortSignal} [opts.signal]
 * @param {(item: object) => void} [opts.onUpdate]
 * @param {number} [opts.intervalMs]
 */
function delayWithAbort(ms, signal) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms)
    if (!signal) return
    if (signal.aborted) {
      clearTimeout(t)
      const err = new Error('Request aborted')
      err.name = 'AbortError'
      reject(err)
      return
    }
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(t)
        const err = new Error('Request aborted')
        err.name = 'AbortError'
        reject(err)
      },
      { once: true },
    )
  })
}

export async function pollGenerationJobItem({
  itemId,
  signal,
  onUpdate,
  intervalMs = 1200,
} = {}) {
  for (;;) {
    const item = await getBatchJobItem({ itemId, signal })
    onUpdate?.(item)
    if (item.status === 'success' || item.status === 'failed') return item
    await delayWithAbort(intervalMs, signal)
  }
}

/**
 * 轮询批任务直到 finished / failed（或后端返回 success/failed）。
 * @param {object} opts
 * @param {string} opts.jobId
 * @param {AbortSignal} [opts.signal]
 * @param {(job: object) => void} [opts.onUpdate]
 * @param {number} [opts.intervalMs]
 */
export async function pollBatchJob({ jobId, signal, onUpdate, intervalMs = 1200 } = {}) {
  for (;;) {
    const data = await getBatchJob({ jobId, signal })
    const job = data?.job || data
    onUpdate?.({ ...(job || {}), items: Array.isArray(data?.items) ? data.items : undefined })
    const st = String(job?.status || '').toLowerCase()
    if (st === 'success' || st === 'failed' || st === 'finished' || st === 'completed') {
      return { ...(job || {}), items: Array.isArray(data?.items) ? data.items : undefined }
    }
    await delayWithAbort(intervalMs, signal)
  }
}

export function getTree({ treeId, signal } = {}) {
  return requestJson(`/api/tree/${encodeURIComponent(treeId)}`, { signal })
}

/** 版本列表（不含 tree_data），来自 GET /api/tree/{tree_id}/history */
export function getTreeHistory({ treeId, signal } = {}) {
  return requestJson(`/api/tree/${encodeURIComponent(treeId)}/history`, { signal })
}

/** 指定版本完整文档（含 tree_data），来自 GET /api/tree/{tree_id}/version/{version} */
export function getTreeVersion({ treeId, version, signal } = {}) {
  return requestJson(
    `/api/tree/${encodeURIComponent(treeId)}/version/${encodeURIComponent(version)}`,
    { signal },
  )
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

