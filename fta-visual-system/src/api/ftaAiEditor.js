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

