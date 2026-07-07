const DEFAULT_AI_EDITOR_BASE_URL = 'http://localhost:8020'

function getBaseUrl() {
  return import.meta?.env?.VITE_FTA_AI_EDITOR_URL || DEFAULT_AI_EDITOR_BASE_URL
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

