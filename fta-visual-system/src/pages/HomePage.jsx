import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { generateTree } from '../api/ftaBackend.js'
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
  const [generating, setGenerating] = useState(false)
  const [generateError, setGenerateError] = useState('')
  const skipTitleBlurRef = useRef(false)
  /** 仅内存：上传的 File 对象，用于本地预览；不写入 localStorage */
  const fileObjectStoreRef = useRef(new Map())

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
      setResultItems(ws.resultItems || [])
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

  const handleSend = async () => {
    const text = chatInput.trim()
    if (!text) return

    const now = Date.now()

    clearProjectReviewed(projectId)

    setGenerating(true)
    setGenerateError('')
    setMessages((prev) => [...prev, { id: `u-${now}`, role: 'user', content: text, at: now }])

    try {
      const resp = await generateTree({ prompt: text })
      const topEvent = resp?.parsed_prompt?.top_event || extractTopEvent(text)
      const treeId = resp?.tree_id || `ft-${Date.now()}`
      const assistantReply = `已为您生成顶事件为${topEvent}的故障树（ID：${treeId}）`

      setMessages((prev) => [
        ...prev,
        { id: `a-${now + 1}`, role: 'assistant', content: assistantReply },
      ])
      setResultItems((prev) => [
        { id: `r-${now}`, faultTreeId: treeId, title: topEvent, createdAt: now },
        ...prev,
      ])
      setChatInput('')
    } catch (e) {
      const topEvent = extractTopEvent(text)
      const faultTreeId = `ft-${Date.now()}`
      const assistantReply = `后端生成失败，已为您创建演示用结果（顶事件：${topEvent}）。`
      setGenerateError(e?.message || '后端生成失败')
      setMessages((prev) => [
        ...prev,
        { id: `a-${now + 1}`, role: 'assistant', content: assistantReply },
      ])
      setResultItems((prev) => [
        { id: `r-${now}`, faultTreeId, title: topEvent, createdAt: now },
        ...prev,
      ])
      setChatInput('')
    } finally {
      setGenerating(false)
    }
  }

  return (
    <div className="home-layout">
      <div className="home-top-row">
        <button
          type="button"
          className="home-back-btn"
          onClick={() => navigate('/')}
        >
          返回
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
                {msg.content}
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
              disabled={generating}
            />
            <button
              type="button"
              className="home-send-btn"
              onClick={handleSend}
              disabled={generating || !chatInput.trim()}
            >
              {generating ? '生成中…' : '发送'}
            </button>
          </div>
          {generateError && <div className="home-chat-error">后端错误：{generateError}</div>}
        </section>

        <section className="home-panel home-panel--right">
          <h2 className="home-panel-title">故障树</h2>
          <p className="home-panel-desc">点击条目可进入故障树编辑画布页面。</p>

          <div className="home-result-list">
            {resultItems.length === 0 && (
              <div className="home-empty">暂无结果。请在中间栏发起生成任务。</div>
            )}
            {resultItems.map((item) => (
              <button
                key={item.id}
                type="button"
                className="home-result-item"
                onClick={() =>
                  navigate(
                    `/fta-viewer?projectId=${encodeURIComponent(projectId)}&faultTreeId=${encodeURIComponent(item.faultTreeId)}`,
                  )
                }
              >
                <span className="home-result-dot" />
                <span className="home-result-text">
                  {item.title}
                  <span className="home-result-id">ID: {item.faultTreeId}</span>
                </span>
                <span className="home-result-link">进入编辑</span>
              </button>
            ))}
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
    </div>
  )
}

export default HomePage
