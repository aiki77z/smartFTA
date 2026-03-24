import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
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
  const [chatInput, setChatInput] = useState('')
  const [projectName, setProjectName] = useState('')
  const [editingProjectName, setEditingProjectName] = useState(false)
  const [messages, setMessages] = useState([...DEFAULT_WORKSPACE_MESSAGES])
  const [resultItems, setResultItems] = useState([])
  const [workspaceReady, setWorkspaceReady] = useState(false)
  const skipTitleBlurRef = useRef(false)

  useEffect(() => {
    if (!projectId) return
    setWorkspaceReady(false)
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
    if (!projectId || !workspaceReady) return
    saveWorkspace(projectId, { files, messages, resultItems })
  }, [projectId, workspaceReady, files, messages, resultItems])

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
    const incoming = selected.map((file, idx) => ({
      id: `${Date.now()}-${idx}`,
      name: file.name,
      size: file.size,
      uploadProgress: 0,
      parseProgress: 0,
      status: 'processing',
    }))
    setFiles((prev) => [...incoming, ...prev])
    if (wasEmpty && incoming.length && projectId) {
      const updated = renameProjectFromFirstFile(projectId, incoming[0].name)
      if (updated) setProjectName(updated.name)
    }
    event.target.value = ''
  }

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

  const handleSend = () => {
    const text = chatInput.trim()
    if (!text) return

    const topEvent = extractTopEvent(text)
    const faultTreeId = `ft-${Date.now()}`
    const assistantReply = `已为您生成顶事件为${topEvent}的故障树`
    const now = Date.now()

    clearProjectReviewed(projectId)

    setMessages((prev) => [
      ...prev,
      { id: `u-${now}`, role: 'user', content: text, at: now },
      { id: `a-${now + 1}`, role: 'assistant', content: assistantReply },
    ])
    setResultItems((prev) => [
      { id: `r-${now}`, faultTreeId, title: topEvent, createdAt: now },
      ...prev,
    ])
    setChatInput('')
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
              <article key={file.id} className="home-file-card">
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
              </article>
            ))}
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
            />
            <button type="button" className="home-send-btn" onClick={handleSend}>
              发送
            </button>
          </div>
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
    </div>
  )
}

export default HomePage
