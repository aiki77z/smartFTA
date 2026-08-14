const DEFAULT_AI_EDITOR_BASE_URL = 'http://localhost:8020'

function getBaseUrl() {
  return import.meta?.env?.VITE_FTA_AI_EDITOR_URL || DEFAULT_AI_EDITOR_BASE_URL
}

async function requestJson(path, { method = 'GET', body, signal } = {}) {
  const base = String(getBaseUrl() || '').replace(/\/+$/, '')
  const resp = await fetch(`${base}${path.startsWith('/') ? path : `/${path}`}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })
  if (!resp.ok) {
    let detail = ''
    try {
      detail = await resp.text()
    } catch {
      detail = ''
    }
    throw new Error(`AssistantAgent 请求失败：${resp.status}${detail ? `\n${detail}` : ''}`)
  }
  return await resp.json()
}

export async function getAssistantSession({ sessionId, signal } = {}) {
  if (!sessionId) throw new Error('sessionId is required')
  return requestJson(`/api/assistant/session/${encodeURIComponent(sessionId)}`, { signal })
}

export async function editFaultTreeWithAi({ instruction, treeJson, selectedFiles = [] }) {
  const base = String(getBaseUrl() || '').replace(/\/+$/, '')
  const resp = await fetch(`${base}/api/fta-edit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      instruction,
      tree_json: treeJson,
      selected_files: selectedFiles,
    }),
  })
  if (!resp.ok) {
    let detail = ''
    try {
      detail = await resp.text()
    } catch {
      detail = ''
    }
    throw new Error(`AI 编辑服务请求失败：${resp.status}${detail ? `\n${detail}` : ''}`)
  }
  return await resp.json()
}

export async function sendAssistantAgentMessage({
  sessionId,
  projectId,
  canvasId,
  message,
  currentTree,
  currentTreeId,
  selectedFileVersionIds = [],
  baselineFileVersionIds = [],
  selectedFiles = [],
  workspaceKbEpoch,
  forceGenerate = false,
  frontendContext = {},
  frontendMessageId,
} = {}) {
  const base = String(getBaseUrl() || '').replace(/\/+$/, '')
  const resp = await fetch(`${base}/api/assistant/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      project_id: projectId,
      canvas_id: canvasId,
      message,
      current_tree: currentTree,
      current_tree_id: currentTreeId,
      selected_file_version_ids: selectedFileVersionIds,
      baseline_file_version_ids: baselineFileVersionIds,
      selected_files: selectedFiles,
      workspace_kb_epoch: workspaceKbEpoch,
      force_generate: forceGenerate,
      frontend_context: frontendContext,
      frontend_message_id: frontendMessageId,
    }),
  })
  if (!resp.ok) {
    let detail = ''
    try {
      detail = await resp.text()
    } catch {
      detail = ''
    }
    throw new Error(`AssistantAgent 请求失败：${resp.status}${detail ? `\n${detail}` : ''}`)
  }
  return await resp.json()
}

export async function getAssistantAgentRun({
  runId,
  sessionId,
  afterEventSeq = 0,
  includeTreeData = false,
  signal,
} = {}) {
  if (!runId) throw new Error('runId is required')
  const params = new URLSearchParams()
  if (sessionId) params.set('session_id', String(sessionId))
  params.set('after_event_seq', String(Math.max(0, Number(afterEventSeq || 0))))
  params.set('include_tree_data', includeTreeData ? 'true' : 'false')
  return requestJson(`/api/assistant/agent-run/${encodeURIComponent(runId)}?${params.toString()}`, { signal })
}

export async function confirmAssistantAgentRun({
  runId,
  sessionId,
  confirmationId,
  confirmationType = 'top_event',
  candidateRef,
  note,
  signal,
} = {}) {
  if (!runId) throw new Error('runId is required')
  return requestJson(`/api/assistant/agent-run/${encodeURIComponent(runId)}/confirm`, {
    method: 'POST',
    signal,
    body: {
      session_id: sessionId,
      confirmation_id: confirmationId,
      confirmation_type: confirmationType,
      candidate_ref: candidateRef,
      ...(note ? { note: String(note) } : null),
    },
  })
}

export async function pollAssistantAgentRun({
  runId,
  sessionId,
  afterEventSeq = 0,
  includeTreeData = true,
  intervalMs = 1500,
  signal,
  onUpdate,
} = {}) {
  let cursor = Math.max(0, Number(afterEventSeq || 0))
  for (;;) {
    const resp = await getAssistantAgentRun({
      runId,
      sessionId,
      afterEventSeq: cursor,
      includeTreeData,
      signal,
    })
    const agentRun = resp?.result?.agent_run || {}
    onUpdate?.(resp, agentRun)
    cursor = Math.max(cursor, Number(agentRun.last_event_seq || agentRun.next_event_seq || 0))
    const status = String(agentRun.status || '').toLowerCase()
    if (['completed', 'failed', 'cancelled', 'waiting_confirmation', 'human_review_required'].includes(status)) {
      const eventCount = Array.isArray(agentRun.events) ? agentRun.events.length : 0
      if (cursor > 0 && eventCount < cursor) {
        const fullResp = await getAssistantAgentRun({
          runId,
          sessionId,
          afterEventSeq: 0,
          includeTreeData,
          signal,
        })
        const fullAgentRun = fullResp?.result?.agent_run || {}
        onUpdate?.(fullResp, fullAgentRun)
        return fullResp
      }
      return resp
    }
    await new Promise((resolve, reject) => {
      const t = window.setTimeout(resolve, intervalMs)
      if (!signal) return
      if (signal.aborted) {
        window.clearTimeout(t)
        reject(Object.assign(new Error('Request aborted'), { name: 'AbortError' }))
      }
      signal.addEventListener(
        'abort',
        () => {
          window.clearTimeout(t)
          reject(Object.assign(new Error('Request aborted'), { name: 'AbortError' }))
        },
        { once: true },
      )
    })
  }
}

export async function truncateAssistantSession({ sessionId, frontendMessageId, keepBeforeIndex } = {}) {
  const base = String(getBaseUrl() || '').replace(/\/+$/, '')
  const resp = await fetch(`${base}/api/assistant/session/${encodeURIComponent(sessionId)}/truncate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      frontend_message_id: frontendMessageId,
      keep_before_index: keepBeforeIndex,
    }),
  })
  if (!resp.ok) {
    let detail = ''
    try {
      detail = await resp.text()
    } catch {
      detail = ''
    }
    throw new Error(`AssistantAgent 回溯同步失败：${resp.status}${detail ? `\n${detail}` : ''}`)
  }
  return await resp.json()
}

