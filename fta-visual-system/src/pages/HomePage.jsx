import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  generateAllTrees,
  generateTree,
  pollBatchJob,
  pollGenerationJobItem,
} from '../api/ftaBackend.js'
import GenerationTaskPanel from '../components/GenerationTaskPanel.jsx'
import {
  clearProjectReviewed,
  DEFAULT_WORKSPACE_MESSAGES,
  ensureProject,
  getProjectById,
  getWorkspace,
  renameProject,
  renameProjectFromFirstFile,
  saveWorkspace,
} from '../utils/projectStore.js'
import { IconChevronLeft } from '../components/icons.jsx'
import ThemeToggle from '../components/ThemeToggle.jsx'
import TaskProgressHistoryModal from '../components/TaskProgressHistoryModal.jsx'
import '../styles/home.css'

const DEFAULT_TOP_EVENT = '示例设备总故障'

function getPreviewKind(fileName, mimeType = '') {
  const lower = (fileName || '').toLowerCase()
  if (lower.endsWith('.txt') || mimeType === 'text/plain') return 'txt'
  if (lower.endsWith('.pdf') || mimeType === 'application/pdf') return 'pdf'
  return 'unsupported'
}

function FileContentPreview({ fileMeta, fileObject, hasAnyFiles, variant = 'inline' }) {
  const [textContent, setTextContent] = useState('')
  const [textError, setTextError] = useState('')
  const [textLoading, setTextLoading] = useState(false)
  const [pdfObjectUrl, setPdfObjectUrl] = useState('')
  const isModal = variant === 'modal'

  const kind = useMemo(
    () => getPreviewKind(fileMeta?.name, fileObject?.type),
    [fileMeta?.name, fileObject?.type],
  )

  useEffect(() => {
    setTextContent('')
    setTextError('')
    setTextLoading(false)
    setPdfObjectUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev)
      return ''
    })

    if (!fileMeta || !fileObject) return undefined

    if (kind === 'txt') {
      setTextLoading(true)
      const reader = new FileReader()
      reader.onload = () => {
        setTextContent(typeof reader.result === 'string' ? reader.result : '')
        setTextLoading(false)
      }
      reader.onerror = () => {
        setTextError('无法读取该文本文件')
        setTextLoading(false)
      }
      reader.readAsText(fileObject, 'UTF-8')
      return undefined
    }

    if (kind === 'pdf') {
      const url = URL.createObjectURL(fileObject)
      setPdfObjectUrl(url)
      return () => {
        URL.revokeObjectURL(url)
      }
    }

    return undefined
  }, [fileMeta?.id, fileObject, kind])

  const phCls = `home-file-preview-placeholder${isModal ? ' home-file-preview-placeholder--modal' : ''}`
  const errCls = `home-file-preview-error${isModal ? ' home-file-preview-error--modal' : ''}`

  if (!fileMeta) {
    return (
      <div className={phCls}>
        {hasAnyFiles
          ? '请点击上方某个文件查看内容。'
          : '请先上传文件，再点击列表中的文件即可在此预览（支持 .txt / .pdf）。'}
      </div>
    )
  }

  if (!fileObject) {
    return (
      <div className={phCls}>
        该文件仅有记录（例如刷新页面后从本地恢复），无法预览原文。请重新上传该文件后再试。
      </div>
    )
  }

  if (kind === 'unsupported') {
    return (
      <div className={phCls}>
        暂不支持预览此格式。当前仅支持 <strong>.txt</strong> 与 <strong>.pdf</strong>。
      </div>
    )
  }

  if (kind === 'txt') {
    if (textLoading) {
      return <div className={phCls}>正在加载文本…</div>
    }
    if (textError) {
      return <div className={errCls}>{textError}</div>
    }
    return (
      <pre
        className={`home-file-preview-text${isModal ? ' home-file-preview-text--modal' : ''}`}
      >
        {textContent || '（文件为空）'}
      </pre>
    )
  }

  if (kind === 'pdf') {
    if (!pdfObjectUrl) {
      return <div className={phCls}>正在加载 PDF…</div>
    }
    return (
      <iframe
        title={`PDF 预览：${fileMeta.name}`}
        className={`home-file-preview-iframe${isModal ? ' home-file-preview-iframe--modal' : ''}`}
        src={`${pdfObjectUrl}#view=FitH`}
      />
    )
  }

  return null
}

function extractTopEvent(input) {
  const text = input.trim()
  if (!text) return DEFAULT_TOP_EVENT
  const match = text.match(/顶事件为\s*["“]?([^"”。，,;\n]+)["”]?/)
  if (match?.[1]) return match[1].trim()
  return DEFAULT_TOP_EVENT
}

/**
 * 与本地存储兼容：旧数据无 status；
 * 任务恢复策略：queued/running 不再直接判定为失败，而是在页面加载后自动恢复轮询。
 */
function normalizeStoredResultItems(items) {
  if (!Array.isArray(items)) return []
  return items
    .filter(Boolean)
    .map((it) => {
      if (!it.status) {
        return {
          ...it,
          status: 'completed',
          progress: 100,
          promptPreview: it.promptPreview || it.title || '',
        }
      }
      if (it.status === 'queued' || it.status === 'running') {
        return {
          ...it,
          // 保持原状态，提示用户正在恢复（具体进度会由恢复轮询覆盖）
          message: it.message || '正在恢复任务进度…',
          stage: it.stage || 'resume',
          error: null,
        }
      }
      return it
    })
}

function HomePage() {
  const navigate = useNavigate()
  const { projectId = '' } = useParams()
  const [files, setFiles] = useState([])
  const [selectedFileId, setSelectedFileId] = useState(null)
  const [chatInput, setChatInput] = useState('')
  const [projectName, setProjectName] = useState('')
  const [editingProjectName, setEditingProjectName] = useState(false)
  const [messages, setMessages] = useState([...DEFAULT_WORKSPACE_MESSAGES])
  const [resultItems, setResultItems] = useState([])
  const [workspaceReady, setWorkspaceReady] = useState(false)
  const [previewModalOpen, setPreviewModalOpen] = useState(false)
  const [batchGenerating, setBatchGenerating] = useState(false)
  const [progressModal, setProgressModal] = useState({ open: false, taskId: '' })
  const skipTitleBlurRef = useRef(false)
  const executionQueueRef = useRef([])
  const drainingRef = useRef(false)
  const resumePollersRef = useRef(new Map())
  /** 仅内存：每个任务最后已消费的 event.seq，用于去重 */
  const taskEventCursorRef = useRef(new Map())
  /** 仅内存：每个任务的历史事件（用于弹窗展示） */
  const taskEventHistoryRef = useRef(new Map())
  /** 仅内存：上传的 File 对象，用于本地预览；不写入 localStorage */
  const fileObjectStoreRef = useRef(new Map())

  const patchResultTask = useCallback((taskId, patch) => {
    setResultItems((prev) => prev.map((t) => (t.id === taskId ? { ...t, ...patch } : t)))
  }, [])

  const formatAgentDisplay = useCallback((agent) => {
    const a = String(agent || '').trim()
    if (!a) return { name: 'Agent', avatar: 'A' }
    if (a === 'LLM#1') return { name: '知识抽取智能体', avatar: '1' }
    if (a === 'LLM#2') return { name: '草稿生成智能体', avatar: '2' }
    if (a === 'LLM#3') return { name: '定向修复智能体', avatar: '3' }
    if (a.includes('召回')) return { name: '召回智能体', avatar: 'R' }
    if (a.includes('修复')) return { name: '修复智能体', avatar: 'F' }
    if (a.includes('校验')) return { name: '校验智能体', avatar: 'V' }
    if (a.includes('调度')) return { name: '调度器', avatar: 'S' }
    if (a.includes('流程')) return { name: '流程控制', avatar: 'P' }
    return { name: a, avatar: a.slice(0, 1).toUpperCase() }
  }, [])

  const appendTaskEventsToChat = useCallback(
    ({ taskId, userPrompt, events }) => {
      if (!taskId || !Array.isArray(events) || events.length === 0) return
      const lastSeq = Number(taskEventCursorRef.current.get(taskId) || 0)
      const incoming = events
        .filter((e) => e && Number(e.seq) > lastSeq)
        .sort((a, b) => Number(a.seq) - Number(b.seq))

      if (incoming.length === 0) return

      const nextLast = Math.max(...incoming.map((e) => Number(e.seq) || 0))
      taskEventCursorRef.current.set(taskId, nextLast)

      const quote = userPrompt
        ? userPrompt.length > 80
          ? `${userPrompt.slice(0, 80)}…`
          : userPrompt
        : ''

      const normalizedIncoming = incoming.map((evt) => {
        const { name, avatar } = formatAgentDisplay(evt.agent)
        return {
          seq: Number(evt.seq) || 0,
          ts: evt.ts || evt.created_at || '',
          agent: name,
          avatar,
          level: String(evt.level || 'INFO').toUpperCase(),
          text: String(evt.text || ''),
          stage: evt.stage || '',
          progress: evt.progress,
        }
      })

      // update per-task history (in-memory)
      const prevHistory = taskEventHistoryRef.current.get(taskId) || []
      const merged = [...prevHistory, ...normalizedIncoming]
      // hard cap to avoid unlimited memory growth
      taskEventHistoryRef.current.set(taskId, merged.slice(-400))

      // chat: keep only ONE message per task, showing latest event
      const latest = normalizedIncoming[normalizedIncoming.length - 1]
      const prefix =
        latest.level === 'ERROR' ? '错误' : latest.level === 'WARNING' ? '警告' : '进度'
      const msg = {
        id: `progress-${taskId}`,
        role: 'assistant',
        kind: 'progress',
        taskId,
        agent: latest.agent,
        avatar: latest.avatar,
        quote,
        content: `${prefix}：${latest.text || ''}`.trim(),
        at: Date.now(),
      }

      setMessages((prev) => {
        const next = []
        let replaced = false
        for (const m of prev) {
          if (m?.kind === 'progress' && m?.taskId === taskId) {
            if (!replaced) next.push(msg)
            replaced = true
          } else {
            next.push(m)
          }
        }
        if (!replaced) next.push(msg)
        return next.slice(-320)
      })
    },
    [formatAgentDisplay],
  )

  useEffect(() => {
    if (!projectId) return
    setWorkspaceReady(false)
    fileObjectStoreRef.current = new Map()
    setSelectedFileId(null)
    ensureProject(projectId)
    const proj = getProjectById(projectId)
    if (proj) setProjectName(proj.name)

    const ws = getWorkspace(projectId)
    if (ws) {
      setFiles(ws.files || [])
      setMessages(
        ws.messages?.length ? ws.messages : [...DEFAULT_WORKSPACE_MESSAGES],
      )
      setResultItems(normalizeStoredResultItems(ws.resultItems || []))
    } else {
      setFiles([])
      setMessages([...DEFAULT_WORKSPACE_MESSAGES])
      setResultItems([])
    }
    setWorkspaceReady(true)
  }, [projectId])

  useEffect(() => {
    if (!selectedFileId) return
    if (!files.some((f) => f.id === selectedFileId)) {
      setSelectedFileId(null)
    }
  }, [files, selectedFileId])

  useEffect(() => {
    if (!projectId || !workspaceReady) return
    saveWorkspace(projectId, { files, messages, resultItems })
  }, [projectId, workspaceReady, files, messages, resultItems])

  // 任务轮询恢复：切换项目/离开页面时终止；在任务列表变化时按需启动新的轮询
  useEffect(() => {
    if (!projectId || !workspaceReady) return undefined
    return () => {
      resumePollersRef.current.forEach((controller) => controller.abort())
      resumePollersRef.current = new Map()
    }
  }, [projectId, workspaceReady])

  useEffect(() => {
    if (!projectId || !workspaceReady) return

    const startResume = (task) => {
      if (!task?.id) return
      if (resumePollersRef.current.has(task.id)) return
      if (task.status !== 'queued' && task.status !== 'running') return
      const hasItem = Boolean(task.itemId)
      const hasJob = Boolean(task.jobId)
      if (!hasItem && !hasJob) return

      const controller = new AbortController()
      resumePollersRef.current.set(task.id, controller)

      patchResultTask(task.id, {
        status: 'running',
        error: null,
        message: task.message || '正在恢复任务进度…',
        stage: task.stage || 'resume',
      })

      ;(async () => {
        try {
          if (hasItem) {
            const finalItem = await pollGenerationJobItem({
              itemId: task.itemId,
              signal: controller.signal,
              onUpdate: (item) => {
                patchResultTask(task.id, {
                  progress: item.progress ?? 0,
                  stage: item.stage || item.status || '',
                  message: item.message || '',
                  title: item.top_event || task.title || '',
                })
                appendTaskEventsToChat({
                  taskId: task.id,
                  userPrompt: task.userPrompt || '',
                  events: item.events || [],
                })
              },
            })
            if (finalItem.status === 'success') {
              patchResultTask(task.id, {
                status: 'completed',
                progress: 100,
                faultTreeId: finalItem.tree_id || task.faultTreeId || null,
                stage: 'completed',
                message: finalItem.message || '生成完成',
                error: null,
              })
            } else if (finalItem.status === 'failed') {
              patchResultTask(task.id, {
                status: 'failed',
                progress: finalItem.progress ?? 100,
                faultTreeId: finalItem.tree_id || task.faultTreeId || null,
                stage: finalItem.stage || 'failed',
                message: finalItem.message || '生成失败',
                error: finalItem.error || '生成失败',
              })
            }
          } else if (hasJob) {
            const finalJob = await pollBatchJob({
              jobId: task.jobId,
              signal: controller.signal,
              onUpdate: (job) => {
                const total = Number(job?.total)
                const success = Number(job?.success)
                const failed = Number(job?.failed)
                const done =
                  (Number.isFinite(success) ? success : 0) + (Number.isFinite(failed) ? failed : 0)
                const progress =
                  Number.isFinite(total) && total > 0 ? Math.round((done / total) * 100) : 0
                patchResultTask(task.id, {
                  progress,
                  stage: job?.stage || job?.status || '',
                  message:
                    job?.message ||
                    (Number.isFinite(total)
                      ? `批量生成中：${done}/${total}（成功 ${success || 0}，失败 ${failed || 0}）`
                      : task.message || ''),
                })
              },
            })
            const st = String(finalJob?.status || '').toLowerCase()
            if (st === 'failed') {
              patchResultTask(task.id, {
                status: 'failed',
                stage: finalJob?.stage || 'failed',
                message: finalJob?.message || '批量任务失败',
                error: finalJob?.error || '批量任务失败',
              })
            } else {
              patchResultTask(task.id, {
                status: 'completed',
                progress: 100,
                stage: finalJob?.stage || 'completed',
                message: finalJob?.message || '批量生成任务已完成',
                error: null,
              })
            }
          }
        } catch (e) {
          if (e?.name === 'AbortError') return
          patchResultTask(task.id, {
            status: 'failed',
            stage: 'failed',
            message: '任务进度恢复失败',
            error: e?.message || '任务进度恢复失败',
          })
        } finally {
          // 任务结束后允许重新启动（例如用户刷新后再次恢复）
          resumePollersRef.current.delete(task.id)
        }
      })()
    }

    resultItems.forEach(startResume)
  }, [projectId, workspaceReady, resultItems, patchResultTask])

  useEffect(() => {
    if (!previewModalOpen) return undefined
    const onKey = (e) => {
      if (e.key === 'Escape') setPreviewModalOpen(false)
    }
    document.addEventListener('keydown', onKey)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
    }
  }, [previewModalOpen])

  useEffect(() => {
    if (!files.length) return
    const timer = setInterval(() => {
      setFiles((prev) =>
        prev.map((item) => {
          if (item.status === 'done') return item

          const upload = Math.min(item.uploadProgress + 12, 100)
          let parse = item.parseProgress
          if (upload >= 100) {
            parse = Math.min(parse + 10, 100)
          }
          const done = upload === 100 && parse === 100

          return {
            ...item,
            uploadProgress: upload,
            parseProgress: parse,
            status: done ? 'done' : 'processing',
          }
        }),
      )
    }, 450)

    return () => clearInterval(timer)
  }, [files.length])

  const finishedCount = useMemo(
    () => files.filter((f) => f.status === 'done').length,
    [files],
  )

  const handleFileChange = (event) => {
    const selected = Array.from(event.target.files || [])
    if (!selected.length) return
    const wasEmpty = files.length === 0
    const batchId = Date.now()
    const incoming = selected.map((file, idx) => {
      const id = `${batchId}-${idx}`
      fileObjectStoreRef.current.set(id, file)
      return {
        id,
        name: file.name,
        size: file.size,
        uploadProgress: 0,
        parseProgress: 0,
        status: 'processing',
      }
    })
    setFiles((prev) => [...incoming, ...prev])
    setSelectedFileId(incoming[0].id)
    if (wasEmpty && incoming.length && projectId) {
      const updated = renameProjectFromFirstFile(projectId, incoming[0].name)
      if (updated) setProjectName(updated.name)
    }
    event.target.value = ''
  }

  const selectedFileMeta = useMemo(
    () => files.find((f) => f.id === selectedFileId) || null,
    [files, selectedFileId],
  )

  const selectedFileObject = selectedFileMeta
    ? fileObjectStoreRef.current.get(selectedFileMeta.id) || null
    : null

  const handleProjectNameSave = () => {
    const updated = renameProject(projectId, projectName)
    if (updated) {
      setProjectName(updated.name)
      setEditingProjectName(false)
      return true
    }
    return false
  }

  const revertProjectNameFromStore = () => {
    const p = getProjectById(projectId)
    if (p) setProjectName(p.name)
    setEditingProjectName(false)
  }

  const runGenerationTask = useCallback(
    async ({ taskId, userPrompt }) => {
      patchResultTask(taskId, {
        status: 'running',
        message: '正在连接后端…',
        progress: 0,
        stage: 'prepare',
      })

      let resp
      try {
        resp = await generateTree({ prompt: userPrompt })
      } catch (e) {
        if (e?.name === 'AbortError') return
        const topEvent = extractTopEvent(userPrompt)
        const faultTreeId = `ft-${Date.now()}`
        patchResultTask(taskId, {
          status: 'completed',
          progress: 100,
          faultTreeId,
          title: topEvent,
          stage: 'demo',
          message: '演示数据（后端不可用）',
          error: null,
        })
        setMessages((prev) => [
          ...prev,
          {
            id: `a-${Date.now()}`,
            role: 'assistant',
            content: `后端生成失败，已为您创建演示用结果（顶事件：${topEvent}）。`,
          },
        ])
        return
      }

      const topEvent = resp?.parsed_prompt?.top_event || extractTopEvent(userPrompt)

      if (resp.mode === 'reuse') {
        const treeId = resp.tree_id
        patchResultTask(taskId, {
          status: 'completed',
          progress: 100,
          faultTreeId: treeId,
          title: topEvent,
          stage: 'reuse',
          message: '已复用已有故障树',
          error: null,
        })
        setMessages((prev) => [
          ...prev,
          {
            id: `a-${Date.now()}`,
            role: 'assistant',
            content: `已为您生成顶事件为${topEvent}的故障树（ID：${treeId}）`,
          },
        ])
        return
      }

      if (resp.mode === 'queued') {
        const itemId = resp.item_id
        if (!itemId) {
          patchResultTask(taskId, {
            status: 'failed',
            progress: 0,
            stage: 'failed',
            message: '无法跟踪进度',
            error: '后端未返回 item_id',
          })
          setMessages((prev) => [
            ...prev,
            {
              id: `a-${Date.now()}`,
              role: 'assistant',
              content: '生成任务已提交，但后端未返回任务标识，无法展示进度。',
            },
          ])
          return
        }
        patchResultTask(taskId, {
          jobId: resp.job_id,
          itemId,
          title: topEvent,
          progress: resp.progress ?? 0,
          stage: 'queued',
          message: '任务已提交，等待执行…',
        })
        let finalItem
        try {
          finalItem = await pollGenerationJobItem({
            itemId,
            onUpdate: (item) => {
              patchResultTask(taskId, {
                progress: item.progress ?? 0,
                stage: item.stage || '',
                message: item.message || '',
                title: item.top_event || topEvent,
              })
              appendTaskEventsToChat({ taskId, userPrompt, events: item.events || [] })
            },
          })
        } catch (pollErr) {
          if (pollErr?.name === 'AbortError') return
          patchResultTask(taskId, {
            status: 'failed',
            progress: 0,
            stage: 'failed',
            message: '无法获取任务进度',
            error: pollErr?.message || '轮询任务失败',
          })
          setMessages((prev) => [
            ...prev,
            {
              id: `a-${Date.now()}`,
              role: 'assistant',
              content: `任务进度查询失败：${pollErr?.message || '未知错误'}`,
            },
          ])
          return
        }

        if (finalItem.status === 'success') {
          const tid = finalItem.tree_id
          patchResultTask(taskId, {
            status: 'completed',
            progress: 100,
            faultTreeId: tid,
            title: finalItem.top_event || topEvent,
            stage: 'completed',
            message: finalItem.message || '生成完成',
            error: null,
          })
          setMessages((prev) => [
            ...prev,
            {
              id: `a-${Date.now()}`,
              role: 'assistant',
              content: `已为您生成顶事件为${finalItem.top_event || topEvent}的故障树（ID：${tid}）`,
            },
          ])
        } else {
          patchResultTask(taskId, {
            status: 'failed',
            progress: finalItem.progress ?? 100,
            faultTreeId: finalItem.tree_id || null,
            stage: finalItem.stage || 'failed',
            message: finalItem.message || '生成失败',
            error: finalItem.error || '生成失败',
          })
          setMessages((prev) => [
            ...prev,
            {
              id: `a-${Date.now()}`,
              role: 'assistant',
              content: `生成失败：${finalItem.error || '未知错误'}`,
            },
          ])
        }
        return
      }

      patchResultTask(taskId, {
        status: 'failed',
        progress: 0,
        stage: 'failed',
        message: '无法解析后端响应',
        error: resp?.mode ? `未知的生成响应：${resp.mode}` : '未知的生成响应',
      })
      setMessages((prev) => [
        ...prev,
        {
          id: `a-${Date.now()}`,
          role: 'assistant',
          content: '后端返回了无法识别的生成结果，请检查接口版本。',
        },
      ])
    },
    [patchResultTask, appendTaskEventsToChat],
  )

  const drainGenerationQueue = useCallback(async () => {
    if (drainingRef.current) return
    drainingRef.current = true
    try {
      while (executionQueueRef.current.length > 0) {
        const job = executionQueueRef.current.shift()
        await runGenerationTask(job)
      }
    } finally {
      drainingRef.current = false
      if (executionQueueRef.current.length > 0) {
        void drainGenerationQueue()
      }
    }
  }, [runGenerationTask])

  const handleTitleBlur = () => {
    if (skipTitleBlurRef.current) {
      skipTitleBlurRef.current = false
      return
    }
    const trimmed = projectName.trim()
    if (!trimmed) {
      revertProjectNameFromStore()
      return
    }
    handleProjectNameSave()
  }

  const handleSend = () => {
    const text = chatInput.trim()
    if (!text) return

    const now = Date.now()
    const taskId = `task-${now}-${Math.random().toString(36).slice(2, 9)}`
    const promptPreview = text.length > 80 ? `${text.slice(0, 80)}…` : text

    clearProjectReviewed(projectId)

    setMessages((prev) => [...prev, { id: `u-${now}`, role: 'user', content: text, at: now }])
    setResultItems((prev) => [
      {
        id: taskId,
        createdAt: now,
        userPrompt: text,
        promptPreview,
        title: extractTopEvent(text),
        faultTreeId: null,
        status: 'queued',
        progress: 0,
        stage: 'queued',
        message: '排队中',
        error: null,
        jobId: null,
        itemId: null,
      },
      ...prev,
    ])
    setChatInput('')

    executionQueueRef.current.push({ taskId, userPrompt: text })
    void drainGenerationQueue()
  }

  const runGenerateAll = useCallback(async () => {
    if (!projectId) return
    if (batchGenerating) return
    setBatchGenerating(true)
    const now = Date.now()
    const taskId = `batch-${now}-${Math.random().toString(36).slice(2, 9)}`
    setResultItems((prev) => [
      {
        id: taskId,
        createdAt: now,
        userPrompt: '',
        promptPreview: '批量生成当前知识库中可识别的全部顶事件故障树',
        title: '批量生成全部故障树',
        faultTreeId: null,
        status: 'running',
        progress: 0,
        stage: 'queued',
        message: '正在提交批量任务…',
        error: null,
        jobId: null,
        itemId: null,
      },
      ...prev,
    ])

    try {
      const resp = await generateAllTrees()
      const jobId = resp?.job_id || resp?.jobId || ''
      if (!jobId) throw new Error('后端未返回 job_id')

      patchResultTask(taskId, {
        jobId,
        progress: 0,
        stage: resp?.status || 'queued',
        message: `已发现 ${resp?.discovered_total ?? '—'} 个顶事件，入队 ${resp?.queued_count ?? '—'} 个`,
      })

      await pollBatchJob({
        jobId,
        onUpdate: (job) => {
          const total = Number(job?.total)
          const success = Number(job?.success)
          const failed = Number(job?.failed)
          const done = (Number.isFinite(success) ? success : 0) + (Number.isFinite(failed) ? failed : 0)
          const pct = Number.isFinite(total) && total > 0 ? Math.round((done / total) * 100) : 0
          patchResultTask(taskId, {
            progress: pct,
            stage: job?.stage || job?.status || '',
            message:
              job?.message ||
              (Number.isFinite(total)
                ? `批量生成中：${done}/${total}（成功 ${success || 0}，失败 ${failed || 0}）`
                : `批量任务进行中（job_id: ${jobId}）`),
          })
        },
      })

      patchResultTask(taskId, {
        status: 'completed',
        progress: 100,
        stage: 'completed',
        message: '批量生成任务已完成（可在右侧任务条目中查看单树任务进度）',
        error: null,
      })
    } catch (e) {
      if (e?.name === 'AbortError') return
      patchResultTask(taskId, {
        status: 'failed',
        progress: 0,
        stage: 'failed',
        message: '批量任务提交失败',
        error: e?.message || '批量生成失败',
      })
      setMessages((prev) => [
        ...prev,
        { id: `a-${Date.now()}`, role: 'assistant', content: `批量生成失败：${e?.message || '未知错误'}` },
      ])
    } finally {
      setBatchGenerating(false)
    }
  }, [projectId, batchGenerating, patchResultTask])

  const openFaultTree = (faultTreeId) => {
    navigate(
      `/fta-viewer?projectId=${encodeURIComponent(projectId)}&faultTreeId=${encodeURIComponent(faultTreeId)}`,
    )
  }

  return (
    <div className="home-layout">
      <div className="home-top-row">
        <button
          type="button"
          className="home-back-btn"
          title="返回"
          aria-label="返回"
          onClick={() => navigate('/')}
        >
          <IconChevronLeft />
        </button>
        <header className="home-header">
          <div>
            {editingProjectName ? (
              <input
                className="home-title-input"
                value={projectName}
                onChange={(e) => setProjectName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    handleProjectNameSave()
                  }
                  if (e.key === 'Escape') {
                    e.preventDefault()
                    skipTitleBlurRef.current = true
                    revertProjectNameFromStore()
                  }
                }}
                onBlur={handleTitleBlur}
                autoFocus
                aria-label="项目名称"
              />
            ) : (
              <button
                type="button"
                className="home-title home-title--editable"
                onClick={() => setEditingProjectName(true)}
              >
                {projectName || '未命名项目'}
              </button>
            )}
            <p className="home-subtitle">知识库构建 · AI 对话生成 · 故障树编辑</p>
          </div>
          <ThemeToggle />
        </header>
      </div>

      <main className="home-main">
        <section className="home-panel home-panel--left">
          <h2 className="home-panel-title">知识库数据上传</h2>
          <p className="home-panel-desc">
            上传设备手册、维修记录等文件。当前为前端演示，进度为模拟展示。
          </p>

          <label className="home-upload-btn">
            选择多个文件
            <input type="file" multiple onChange={handleFileChange} />
          </label>

          <p className="home-upload-summary">
            共 {files.length} 个文件，已完成 {finishedCount} 个
          </p>

          <div className="home-file-list">
            {files.length === 0 && (
              <div className="home-empty">暂无文件。请先上传设备资料。</div>
            )}
            {files.map((file) => (
              <button
                key={file.id}
                type="button"
                className={`home-file-card${
                  file.id === selectedFileId ? ' home-file-card--selected' : ''
                }`}
                onClick={() => setSelectedFileId(file.id)}
              >
                <div className="home-file-head">
                  <span className="home-file-name">{file.name}</span>
                  <span className="home-file-size">
                    {(file.size / 1024).toFixed(1)} KB
                  </span>
                </div>
                <div className="home-progress-row">
                  <span>上传进度</span>
                  <span>{file.uploadProgress}%</span>
                </div>
                <div className="home-progress-track">
                  <div
                    className="home-progress-fill"
                    style={{ width: `${file.uploadProgress}%` }}
                  />
                </div>
                <div className="home-progress-row">
                  <span>解析进度</span>
                  <span>{file.parseProgress}%</span>
                </div>
                <div className="home-progress-track home-progress-track--parse">
                  <div
                    className="home-progress-fill home-progress-fill--parse"
                    style={{ width: `${file.parseProgress}%` }}
                  />
                </div>
              </button>
            ))}
          </div>

          <div
            className="home-file-preview-section home-file-preview-section--expandable"
            role="button"
            tabIndex={0}
            aria-label="文件内容预览，点击放大查看"
            onClick={() => setPreviewModalOpen(true)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                setPreviewModalOpen(true)
              }
            }}
          >
            <h3 className="home-file-preview-title">文件内容预览</h3>
            <p className="home-file-preview-hint">
              支持 .txt 与 .pdf；下方可滚动查看全文。点击本区域可放大查看。
            </p>
            <div className="home-file-preview-body">
              <FileContentPreview
                fileMeta={selectedFileMeta}
                fileObject={selectedFileObject}
                hasAnyFiles={files.length > 0}
              />
            </div>
          </div>
        </section>

        <section className="home-panel home-panel--center">
          <h2 className="home-panel-title">AI 对话生成</h2>
          <p className="home-panel-desc">
            建议输入：为我创建一棵顶事件为“某设备故障现象”的故障树。
          </p>

          <div className="home-chat-list">
            {messages.map((msg) => (
              <div
                key={msg.id}
                className={`home-chat-item ${
                  msg.role === 'user' ? 'home-chat-item--user' : 'home-chat-item--assistant'
                }`}
              >
                {msg.kind === 'progress' ? (
                  <div className="home-chat-progress">
                    <div className="home-chat-progress-head">
                      <span className="home-chat-progress-avatar" aria-hidden>
                        {msg.avatar || 'A'}
                      </span>
                      <span className="home-chat-progress-agent">{msg.agent || 'Agent'}</span>
                      {msg.taskId ? (
                        <span className="home-chat-progress-task" title={msg.taskId}>
                          #{String(msg.taskId).slice(-6)}
                        </span>
                      ) : null}
                    </div>
                    {msg.quote ? <div className="home-chat-progress-quote">引用：{msg.quote}</div> : null}
                    <button
                      type="button"
                      className="home-chat-progress-body home-chat-progress-body--btn"
                      onClick={() => setProgressModal({ open: true, taskId: msg.taskId || '' })}
                      title="点击查看该任务的历史进度记录"
                    >
                      {msg.content}
                      <span className="home-chat-progress-more">查看历史</span>
                    </button>
                  </div>
                ) : (
                  msg.content
                )}
              </div>
            ))}
          </div>

          <div className="home-chat-input-wrap">
            <input
              className="home-chat-input"
              value={chatInput}
              onChange={(e) => setChatInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSend()}
              placeholder="请输入你的需求..."
            />
            <button
              type="button"
              className="home-send-btn"
              onClick={handleSend}
              disabled={!chatInput.trim()}
            >
              发送
            </button>
          </div>
        </section>

        <section className="home-panel home-panel--right">
          <h2 className="home-panel-title">生成任务与结果</h2>
          <p className="home-panel-desc">
            任务按队列依次执行；生成中也可继续发起新任务。完成后点击条目进入编辑画布。
          </p>

          <div style={{ display: 'flex', gap: 10, marginBottom: 10, flexWrap: 'wrap' }}>
            <button
              type="button"
              className="home-send-btn"
              onClick={runGenerateAll}
              disabled={batchGenerating}
              title="调用 /api/batch/generate-all 批量生成"
            >
              批量生成全部故障树
            </button>
          </div>

          <div className="home-result-list">
            <GenerationTaskPanel tasks={resultItems} onOpenTree={openFaultTree} />
          </div>
        </section>
      </main>

      {previewModalOpen && (
        <div
          className="home-preview-modal-overlay"
          role="presentation"
          onClick={() => setPreviewModalOpen(false)}
        >
          <div
            className="home-preview-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="home-preview-modal-title"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="home-preview-modal-header">
              <h2 className="home-preview-modal-title" id="home-preview-modal-title">
                文件预览
                {selectedFileMeta?.name
                  ? ` — ${selectedFileMeta.name}`
                  : ''}
              </h2>
              <button
                type="button"
                className="home-preview-modal-close"
                aria-label="关闭预览"
                onClick={() => setPreviewModalOpen(false)}
              >
                ×
              </button>
            </header>
            <div className="home-preview-modal-body">
              <div className="home-preview-modal-inner">
                <FileContentPreview
                  variant="modal"
                  fileMeta={selectedFileMeta}
                  fileObject={selectedFileObject}
                  hasAnyFiles={files.length > 0}
                />
              </div>
            </div>
          </div>
        </div>
      )}

      <TaskProgressHistoryModal
        open={!!progressModal.open}
        onClose={() => setProgressModal({ open: false, taskId: '' })}
        taskId={progressModal.taskId}
        title={resultItems.find((t) => t.id === progressModal.taskId)?.title || ''}
        quote={resultItems.find((t) => t.id === progressModal.taskId)?.userPrompt || ''}
        events={taskEventHistoryRef.current.get(progressModal.taskId) || []}
      />
    </div>
  )
}

export default HomePage
