import './generation-task-panel.css'

function stageLabel(stage) {
  if (!stage) return ''
  const map = {
    queued: '排队',
    prepare: '准备',
    tree_record: '创建记录',
    completed: '完成',
    reuse: '复用',
    failed: '失败',
    accelerated: '加速',
  }
  return map[stage] || stage
}

/**
 * @param {object} props
 * @param {Array<{
 *   id: string,
 *   title: string,
 *   promptPreview?: string,
 *   faultTreeId?: string | null,
 *   status: 'queued' | 'running' | 'completed' | 'failed',
 *   progress?: number,
 *   stage?: string,
 *   message?: string,
 *   error?: string | null,
 * }>} props.tasks
 * @param {(faultTreeId: string) => void} props.onOpenTree
 */
export default function GenerationTaskPanel({ tasks, onOpenTree }) {
  return (
    <div className="gen-task-list">
      {tasks.length === 0 && (
        <div className="home-empty">暂无任务。请在下方旧版对话中发起生成任务。</div>
      )}
      {tasks.map((task) => {
        const pct = Math.max(0, Math.min(100, Number(task.progress) || 0))
        const done = task.status === 'completed' && task.faultTreeId
        const failed = task.status === 'failed'
        const queued = task.status === 'queued'
        const running = task.status === 'running'
        const clickable = Boolean(done)
        const stageText = stageLabel(task.stage)
        const detail = [task.message, stageText && `阶段：${stageText}`].filter(Boolean).join(' · ')

        return (
          <button
            key={task.id}
            type="button"
            className={`gen-task-card${clickable ? ' gen-task-card--clickable' : ''}${
              failed ? ' gen-task-card--failed' : ''
            }`}
            disabled={!clickable}
            onClick={() => {
              if (clickable && task.faultTreeId) onOpenTree(task.faultTreeId)
            }}
            aria-busy={running || undefined}
          >
            <div className="gen-task-head">
              <span
                className={`gen-task-dot${
                  failed
                    ? ' gen-task-dot--fail'
                    : done
                      ? ' gen-task-dot--done'
                      : running
                        ? ' gen-task-dot--running'
                        : queued
                          ? ' gen-task-dot--queued'
                          : ' gen-task-dot--queued'
                }`}
                aria-hidden
              />
              <div className="gen-task-main">
                <div className="gen-task-title">{task.title || '故障树生成'}</div>
                {task.promptPreview ? (
                  <div className="gen-task-prompt">{task.promptPreview}</div>
                ) : null}
                {done && task.faultTreeId ? (
                  <div className="gen-task-meta">ID: {task.faultTreeId}</div>
                ) : null}
              </div>
            </div>

            {queued ? (
              <div className="gen-task-row">
                <span className="gen-task-stage">等待上一任务完成后开始</span>
              </div>
            ) : null}

            {running || (failed && pct > 0 && pct < 100) ? (
              <>
                <div className="gen-task-row">
                  <span className="gen-task-stage" title={detail || task.message}>
                    {running ? detail || '生成中…' : task.message || ''}
                  </span>
                  <span className="gen-task-pct">{pct}%</span>
                </div>
                <div className="gen-task-track" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
                  <div className="gen-task-fill" style={{ width: `${pct}%` }} />
                </div>
              </>
            ) : null}

            {failed && task.error ? <div className="gen-task-err">{task.error}</div> : null}

            {clickable ? (
              <div className="gen-task-actions">
                <span className="gen-task-link">进入编辑</span>
              </div>
            ) : null}
          </button>
        )
      })}
    </div>
  )
}
