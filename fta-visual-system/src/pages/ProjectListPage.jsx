import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import ThemeToggle from '../components/ThemeToggle.jsx'
import { createProject, listProjectsSortedForListPage } from '../utils/projectStore.js'
import '../styles/project-list.css'

function formatTime(ts) {
  if (!ts) return '暂无'
  return new Date(ts).toLocaleString()
}

const STATUS_CLASS = {
  待上传: 'project-status--upload',
  构建中: 'project-status--build',
  待审核: 'project-status--review',
  已审核: 'project-status--done',
}

function ProjectListPage() {
  const navigate = useNavigate()
  const [refreshKey, setRefreshKey] = useState(0)

  const projects = useMemo(() => listProjectsSortedForListPage(), [refreshKey])

  const handleCreate = () => {
    const project = createProject()
    setRefreshKey((k) => k + 1)
    navigate(`/project/${project.id}`)
  }

  return (
    <div className="project-page">
      <div className="project-hero-row">
        <img
          className="project-hero-icon"
          src="/故障树分析.svg"
          alt="故障树分析"
          aria-hidden="true"
        />
        <h1 className="project-hero-title">故障树智能构建系统</h1>
      </div>
      <p className="project-hero-desc">
        一轮数据上传 → AI 故障树构建 → 专家修改与审核，全流程以「项目」组织
      </p>

      <div className="project-toolbar">
        <h2 className="project-section-label">我的项目</h2>
        <div className="project-toolbar-actions">
          <ThemeToggle />
          <button
            type="button"
            className="project-fab"
            title="新建项目"
            aria-label="新建项目"
            onClick={handleCreate}
          >
            +
          </button>
        </div>
      </div>

      <div className="project-grid">
        {projects.length === 0 && (
          <div className="project-empty project-empty--wide">
            暂无项目，点击右上角「+」新建一个项目开始。
          </div>
        )}
        {projects.map((project) => (
          <button
            key={project.id}
            type="button"
            className="project-card"
            onClick={() => navigate(`/project/${project.id}`)}
          >
            <div className="project-card-head">
              <span className="project-card-name">{project.name}</span>
              <span
                className={`project-status ${
                  STATUS_CLASS[project.workflowStatus] || 'project-status--upload'
                }`}
              >
                {project.workflowStatus || '待上传'}
              </span>
            </div>
            <dl className="project-card-body">
              <div className="project-card-row">
                <dt>创建时间</dt>
                <dd>{formatTime(project.createdAt)}</dd>
              </div>
              <div className="project-card-row">
                <dt>上一次对话</dt>
                <dd>{formatTime(project.lastChatAt)}</dd>
              </div>
            </dl>
          </button>
        ))}
      </div>
    </div>
  )
}

export default ProjectListPage
