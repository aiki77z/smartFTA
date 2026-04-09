import './task-progress-history-modal.css'
import SwimlaneActivityDiagram from './SwimlaneActivityDiagram.jsx'

function formatTime(ts) {
  if (!ts) return ''
  try {
    // backend uses datetime; frontend may receive ISO string or object
    const d = typeof ts === 'string' ? new Date(ts) : new Date(ts)
    if (Number.isNaN(d.getTime())) return String(ts)
    return d.toLocaleString()
  } catch {
    return String(ts)
  }
}

export default function TaskProgressHistoryModal({
  open,
  onClose,
  taskId,
  title,
  quote,
  events = [],
}) {
  if (!open) return null

  const safeEvents = Array.isArray(events) ? events : []

  return (
    <div className="tph-overlay" role="presentation" onClick={onClose}>
      <div
        className="tph-modal"
        role="dialog"
        aria-modal="true"
        aria-label="任务进度记录"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="tph-header">
          <div className="tph-header-main">
            <div className="tph-title">任务进度记录</div>
            <div className="tph-meta">
              {title ? <span className="tph-meta-pill">{title}</span> : null}
              {taskId ? (
                <span className="tph-meta-mono" title={taskId}>
                  {taskId}
                </span>
              ) : null}
            </div>
            {quote ? <div className="tph-quote">引用：{quote}</div> : null}
          </div>
          <button type="button" className="tph-close" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </header>

        <div className="tph-body tph-body--split">
          <div className="tph-left">
            {safeEvents.length === 0 ? (
              <div className="tph-empty">暂无历史进度记录。</div>
            ) : (
              <div className="tph-list">
                {safeEvents.map((evt) => (
                  <div key={`${evt.seq || ''}-${evt.ts || ''}`} className="tph-item">
                    <div className="tph-item-head">
                      <span className="tph-avatar" aria-hidden>
                        {evt.avatar || 'A'}
                      </span>
                      <span className="tph-agent">{evt.agent || 'Agent'}</span>
                      {evt.level ? (
                        <span className={`tph-level tph-level--${String(evt.level).toLowerCase()}`}>
                          {String(evt.level).toUpperCase()}
                        </span>
                      ) : null}
                      <span className="tph-time">{formatTime(evt.ts)}</span>
                      {Number.isFinite(Number(evt.progress)) ? (
                        <span className="tph-progress">{Number(evt.progress)}%</span>
                      ) : null}
                    </div>
                    <div className="tph-text">{evt.text || ''}</div>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="tph-right">
            <div className="tph-diagram">
              <SwimlaneActivityDiagram events={safeEvents} compact />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

