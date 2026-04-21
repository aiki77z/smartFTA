import './knowledge-base-panel.css'
import { KB_BUILD_STAGES, computeKbTimelineState } from './knowledgeBaseConstants.js'

/**
 * @param {object} props
 * @param {object | null} [props.job] 来自 FTA-KB 的 job 数据（GET /api/kb/jobs/{job_id}）
 * @param {'idle' | 'parsing' | 'entity' | 'relation' | 'graph' | 'complete'} [props.phase] 兼容旧的前端演示模式
 * @param {() => void} [props.onOpenGraph] 点击“导入图谱”时打开图谱弹窗
 * @param {boolean} [props.hydrating] 页面返回后正在恢复 job 状态：抑制“错误”闪烁
 */
export default function KnowledgeBasePanel({ job, phase, onOpenGraph, hydrating }) {
  const tl = computeKbTimelineState(job, phase)

  return (
    <div className="home-kb">
      <h3 className="home-kb-title">知识库构建进度</h3>
      <p className="home-kb-desc">
        {job
          ? '已接入 FTA-KB：上传后会在后端启动任务，横轴表示各阶段推进情况。'
          : '上传并完成本地解析后，将依次进入实体抽取、关系抽取与图谱构建（演示模式）。'}
      </p>

      <div className="home-kb-timeline" aria-label="知识库构建阶段">
        <div className="home-kb-timeline-track" aria-hidden>
          <div className="home-kb-timeline-track-bg" />
          <div
            className="home-kb-timeline-track-fill"
            style={{ width: `${tl.fillPercent}%` }}
          />
        </div>
        <div className="home-kb-timeline-nodes">
          {KB_BUILD_STAGES.map((s, i) => {
            const done = tl.allDone || tl.stageIndex > i
            const active = !tl.allDone && tl.stageIndex === i
            const pending = tl.stageIndex >= 0 && tl.stageIndex < i && !tl.allDone
            const idle = tl.stageIndex < 0 && !job
            const clickable = s.id === 'sync' && typeof onOpenGraph === 'function'

            return (
              <button
                key={s.id}
                type="button"
                className={`home-kb-tnode${clickable ? ' home-kb-tnode--clickable' : ''}`}
                onClick={clickable ? onOpenGraph : undefined}
                title={clickable ? '打开知识图谱' : undefined}
                aria-label={clickable ? '打开知识图谱' : undefined}
                disabled={!clickable}
              >
                <div
                  className={`home-kb-tnode-dot${done ? ' home-kb-tnode-dot--done' : ''}${
                    active ? ' home-kb-tnode-dot--active' : ''
                  }${pending || idle ? ' home-kb-tnode-dot--muted' : ''}`}
                >
                  {done ? '✓' : i + 1}
                </div>
                <span className="home-kb-tnode-label">{s.label}</span>
              </button>
            )
          })}
        </div>
      </div>

      {job ? (
        <div className="home-kb-live home-kb-live--compact">
          <div className="home-kb-live-msg" role="status">
            <strong>状态</strong>：{tl.st || '—'}；<strong>阶段</strong>：{tl.stage || '—'}；<strong>进度</strong>：
            {Number.isFinite(Number(tl.progress)) ? `${Math.round(Number(tl.progress))}%` : '—'}
          </div>
          {tl.message ? (
            <div className="home-kb-live-msg" role="status">
              {tl.message}
            </div>
          ) : null}
          {!hydrating && tl.error ? <div className="home-kb-live-err">错误：{String(tl.error)}</div> : null}
        </div>
      ) : null}
    </div>
  )
}
