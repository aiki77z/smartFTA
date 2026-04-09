const PROJECTS_KEY = 'fta-projects'
const WORKSPACES_KEY = 'fta-project-workspaces'

function readProjects() {
  if (typeof window === 'undefined') return []
  try {
    const raw = localStorage.getItem(PROJECTS_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function saveProjects(projects) {
  if (typeof window === 'undefined') return
  localStorage.setItem(PROJECTS_KEY, JSON.stringify(projects))
}

function readWorkspaces() {
  if (typeof window === 'undefined') return {}
  try {
    const raw = localStorage.getItem(WORKSPACES_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function saveWorkspaces(map) {
  if (typeof window === 'undefined') return
  localStorage.setItem(WORKSPACES_KEY, JSON.stringify(map))
}

/**
 * 「空白项目 N」的 N：仅统计当前仍命名为「空白项目+数字」的项目，取最大序号 + 1。
 * 与项目总数无关；已有若干非空白项目时，第一个空白项目仍为「空白项目1」。
 */
function nextBlankProjectIndex() {
  const projects = readProjects()
  let max = 0
  for (const p of projects) {
    const name = typeof p.name === 'string' ? p.name.trim() : ''
    const m = /^空白项目(\d+)$/.exec(name)
    if (m) {
      const n = parseInt(m[1], 10)
      if (Number.isFinite(n)) max = Math.max(max, n)
    }
  }
  return max + 1
}

/** 默认欢迎语，与工作区初始一致 */
export const DEFAULT_WORKSPACE_MESSAGES = [
  {
    id: 'welcome',
    role: 'assistant',
    content:
      '欢迎使用故障树智能生成助手。你可以输入：为我创建一棵顶事件为“液压泵无法启动”的故障树。',
  },
]

export function getWorkspace(projectId) {
  const all = readWorkspaces()
  const ws = all[projectId]
  if (!ws) return null
  return {
    files: Array.isArray(ws.files) ? ws.files : [],
    messages: Array.isArray(ws.messages) ? ws.messages : [...DEFAULT_WORKSPACE_MESSAGES],
    resultItems: Array.isArray(ws.resultItems) ? ws.resultItems : [],
  }
}

export function saveWorkspace(projectId, workspace) {
  const all = readWorkspaces()
  all[projectId] = {
    files: workspace.files || [],
    messages: workspace.messages || [],
    resultItems: workspace.resultItems || [],
  }
  saveWorkspaces(all)
  syncProjectFromWorkspace(projectId, all[projectId])
}

function patchProject(projectId, patch) {
  const projects = readProjects()
  let found = false
  const next = projects.map((p) => {
    if (p.id !== projectId) return p
    found = true
    return { ...p, ...patch, updatedAt: Date.now() }
  })
  if (found) saveProjects(next)
}

export function syncProjectFromWorkspace(projectId, workspace) {
  const project = getProjectById(projectId)
  if (!project) return

  const files = workspace.files || []
  const messages = workspace.messages || []
  const resultItems = workspace.resultItems || []
  const userMsgs = messages.filter((m) => m.role === 'user')
  const lastChatAt =
    userMsgs.length > 0 ? Math.max(...userMsgs.map((m) => Number(m.at) || 0)) : null

  let workflowStatus = '待上传'
  if (project.reviewed) workflowStatus = '已审核'
  else if (resultItems.length > 0) workflowStatus = '待审核'
  else if (files.length > 0 || userMsgs.length > 0) workflowStatus = '构建中'

  patchProject(projectId, {
    lastChatAt: lastChatAt || null,
    workflowStatus,
  })
}

export function listProjectsSortedForListPage() {
  const projects = readProjects().map((p) => ({
    ...p,
    lastChatAt: p.lastChatAt ?? null,
    workflowStatus: p.workflowStatus ?? '待上传',
    reviewed: p.reviewed ?? false,
  }))
  return [...projects].sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0))
}

export function listProjects() {
  return readProjects().sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
}

export function getProjectById(projectId) {
  return readProjects().find((p) => p.id === projectId) || null
}

export function createProject() {
  const now = Date.now()
  const blankIndex = nextBlankProjectIndex()
  const project = {
    id: `proj-${now}-${Math.random().toString(36).slice(2, 10)}`,
    name: `空白项目${blankIndex}`,
    blankSeq: blankIndex,
    createdAt: now,
    updatedAt: now,
    lastChatAt: null,
    workflowStatus: '待上传',
    reviewed: false,
  }
  const projects = readProjects()
  projects.unshift(project)
  saveProjects(projects)
  return project
}

export function ensureProject(projectId) {
  const found = getProjectById(projectId)
  if (found) return found
  const fallback = {
    id: projectId,
    name: '空白项目0',
    createdAt: Date.now(),
    updatedAt: Date.now(),
    lastChatAt: null,
    workflowStatus: '待上传',
    reviewed: false,
  }
  const projects = readProjects()
  projects.unshift(fallback)
  saveProjects(projects)
  return fallback
}

export function renameProject(projectId, name) {
  const trimmed = (name || '').trim()
  if (!trimmed) return null
  const projects = readProjects()
  let updated = null
  const next = projects.map((project) => {
    if (project.id !== projectId) return project
    updated = { ...project, name: trimmed, updatedAt: Date.now() }
    return updated
  })
  if (updated) saveProjects(next)
  return updated
}

/** 若当前名称仍为「空白项目」，则用首次上传的文件名作为项目名 */
export function renameProjectFromFirstFile(projectId, fileName) {
  const safeName = (fileName || '').trim()
  if (!safeName) return null
  const project = getProjectById(projectId)
  if (!project) return null
  if (!/^空白项目\d+$/.test(project.name)) return null
  return renameProject(projectId, safeName)
}

export function markProjectReviewed(projectId) {
  if (!projectId) return
  patchProject(projectId, { reviewed: true, workflowStatus: '已审核' })
}

/** 生成新的故障树结果后，视为需再次审核 */
export function clearProjectReviewed(projectId) {
  if (!projectId) return
  patchProject(projectId, { reviewed: false })
}
