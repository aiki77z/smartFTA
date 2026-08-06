const DEFAULT_BASE_URL = 'http://localhost:8000'

function getBaseUrl() {
  const envUrl = import.meta?.env?.VITE_FTA_BACKEND_URL
  return (envUrl || DEFAULT_BASE_URL).replace(/\/+$/, '')
}

export function getAgentBackendBaseUrl() {
  return getBaseUrl()
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
    if (e?.name === 'AbortError' || msg.includes('aborted') || msg.includes('signal is aborted')) {
      const err = new Error('Request aborted')
      err.name = 'AbortError'
      throw err
    }
    throw e
  }

  if (res.ok) return await res.json()

  let data
  try {
    data = await res.json()
  } catch {
    data = { error: { message: await res.text().catch(() => res.statusText) } }
  }
  const apiError = data?.error || data?.detail || data
  const message =
    apiError?.message ||
    (typeof apiError === 'string' ? apiError : JSON.stringify(apiError || data)) ||
    res.statusText
  const err = new Error(`Backend API error (${res.status}): ${message}`)
  err.status = res.status
  err.detail = data
  err.code = apiError?.code
  err.retryable = Boolean(apiError?.retryable)
  throw err
}

function cleanIds(values) {
  return Array.isArray(values)
    ? values.map((value) => String(value || '').trim()).filter(Boolean)
    : []
}

export function createAgentRun({
  prompt,
  selectedFileVersionIds,
  sessionId,
  projectId,
  canvasId,
  treeId,
  treeVersion,
  maxDepth,
  sync = false,
  options,
  signal,
} = {}) {
  const body = {
    task_type: 'generate_fault_tree',
    prompt: String(prompt || ''),
    selected_file_version_ids: cleanIds(selectedFileVersionIds),
    sync: Boolean(sync),
  }
  if (sessionId) body.session_id = String(sessionId)
  if (projectId) body.project_id = String(projectId)
  if (canvasId) body.canvas_id = String(canvasId)
  if (treeId) body.tree_id = String(treeId)
  if (treeVersion !== undefined && treeVersion !== null) body.tree_version = Number(treeVersion)
  if (maxDepth !== undefined && maxDepth !== null) body.max_depth = Number(maxDepth)
  if (options && typeof options === 'object' && !Array.isArray(options)) body.options = options
  return requestJson('/api/agent/run', { method: 'POST', body, signal })
}

export function getAgentRun({
  runId,
  afterEventSeq = 0,
  includeTreeData = false,
  signal,
} = {}) {
  if (!runId) throw new Error('runId is required')
  const params = new URLSearchParams()
  params.set('after_event_seq', String(Math.max(0, Number(afterEventSeq || 0))))
  params.set('include_tree_data', includeTreeData ? 'true' : 'false')
  return requestJson(`/api/agent/run/${encodeURIComponent(runId)}?${params.toString()}`, { signal })
}

export function confirmAgentRun({
  runId,
  confirmationId,
  confirmationType = 'top_event',
  candidateRef,
  note,
  signal,
} = {}) {
  if (!runId) throw new Error('runId is required')
  return requestJson(`/api/agent/run/${encodeURIComponent(runId)}/confirm`, {
    method: 'POST',
    body: {
      confirmation_id: String(confirmationId || ''),
      confirmation_type: String(confirmationType || 'top_event'),
      candidate_ref: String(candidateRef || ''),
      ...(note ? { note: String(note) } : null),
    },
    signal,
  })
}

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

export async function pollAgentRun({
  runId,
  afterEventSeq = 0,
  includeTreeData = false,
  signal,
  onUpdate,
  intervalMs = 1200,
} = {}) {
  let cursor = Math.max(0, Number(afterEventSeq || 0))
  for (;;) {
    const data = await getAgentRun({ runId, afterEventSeq: cursor, includeTreeData, signal })
    const events = Array.isArray(data?.events) ? data.events : []
    for (const event of events) {
      cursor = Math.max(cursor, Number(event?.event_seq || 0))
    }
    onUpdate?.(data)
    const status = String(data?.status || '').toLowerCase()
    if (status === 'completed' || status === 'failed' || status === 'cancelled') return data
    await delayWithAbort(intervalMs, signal)
  }
}

