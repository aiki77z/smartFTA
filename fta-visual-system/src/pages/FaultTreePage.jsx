import { useCallback, useMemo, useRef, useState } from 'react'
import FaultTreeCanvas from '../components/fta/FaultTreeCanvas.jsx'
import rawFtaSample from '../raw-FTA/raw-FTA.json'
import { parseRawFtaJson } from '../utils/ftaParser.js'
import '../styles/fta.css'

function normalizeToGraph(raw) {
  if (!raw) return { nodes: [], edges: [] }

  if (Array.isArray(raw.nodeList) && Array.isArray(raw.linkList)) {
    return parseRawFtaJson(raw)
  }

  if (Array.isArray(raw.nodes) && Array.isArray(raw.edges)) {
    return {
      nodes: raw.nodes.map((n) => ({
        id: String(n.id),
        label: n.label || n.name || n.title || String(n.id),
        type: n.type || 'event',
        level: n.level ?? n.depth ?? 0,
        meta: n.meta ?? n,
      })),
      edges: raw.edges.map((e, idx) => ({
        id: e.id ? String(e.id) : `e-${idx}`,
        source: String(e.source),
        target: String(e.target),
        relation: e.relation || e.gate || '',
        meta: e.meta ?? e,
      })),
    }
  }

  return { nodes: [], edges: [] }
}

function FaultTreePage() {
  const [rawJsonText, setRawJsonText] = useState(
    JSON.stringify(rawFtaSample, null, 2),
  )
  const [graphData, setGraphData] = useState(() =>
    normalizeToGraph(rawFtaSample),
  )
  const [error, setError] = useState('')
  const [selectedNode, setSelectedNode] = useState(null)
  const [exporting, setExporting] = useState(false)
  const canvasRef = useRef(null)
  const canvasActionsRef = useRef(null)

  const handleJsonChange = useCallback((e) => {
    setRawJsonText(e.target.value)
  }, [])

  const handleApplyJson = useCallback(() => {
    try {
      const parsed = JSON.parse(rawJsonText)
      const normalized = normalizeToGraph(parsed)
      setGraphData(normalized)
      setError('')
    } catch (err) {
      setError(`JSON 解析失败：${err.message}`)
    }
  }, [rawJsonText])

  const handleDownloadJson = useCallback(() => {
    const blob = new Blob([rawJsonText], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'fault-tree.json'
    a.click()
    URL.revokeObjectURL(url)
  }, [rawJsonText])

  const handleDownloadImage = useCallback(async () => {
    if (!canvasActionsRef.current?.exportImage) return
    try {
      setExporting(true)
      await new Promise((resolve) => setTimeout(resolve, 50))
      const dataUrl = await canvasActionsRef.current.exportImage()
      if (!dataUrl) throw new Error('export returned empty')
      const link = document.createElement('a')
      link.href = dataUrl
      link.download = 'fault-tree.png'
      link.click()
    } catch (err) {
      console.error(err)
      setError('图片导出失败，请重试')
    } finally {
      setExporting(false)
    }
  }, [])

  const hasGraph = useMemo(
    () => graphData.nodes.length > 0,
    [graphData.nodes.length],
  )

  const selectedMeta = selectedNode?.data?.meta

  return (
    <div className="fta-layout">
      <header className="fta-header">
        <div>
          <h1 className="fta-title">故障树可视化编辑（演示版）</h1>
          <p className="fta-subtitle">
            演示链路：知识库构建结果 JSON → 故障树图形化展示 → 图片 / JSON 导出
          </p>
        </div>
        <div className="fta-header-actions">
          <button type="button" className="fta-btn ghost" onClick={handleDownloadJson}>
            下载当前 JSON
          </button>
          <button
            type="button"
            className="fta-btn primary"
            onClick={handleDownloadImage}
            disabled={!hasGraph}
          >
            下载故障树图片
          </button>
        </div>
      </header>

      <main className="fta-main">
        <section className="fta-side">
          <h2 className="fta-section-title">知识库结果 JSON</h2>
          <p className="fta-section-desc">
            默认加载 raw-FTA.json，你也可以将类似格式的内容粘贴到这里，然后点击“应用到画布”。
          </p>
          <textarea
            className="fta-json-input"
            value={rawJsonText}
            onChange={handleJsonChange}
            spellCheck={false}
          />
          <button type="button" className="fta-btn full" onClick={handleApplyJson}>
            应用到画布
          </button>
          {error && <p className="fta-error-text">{error}</p>}
        </section>

        <section className="fta-canvas-section">
          <h2 className="fta-section-title">故障树画布</h2>
          <p className="fta-section-desc">
            支持拖拽视图、缩放、点击节点查看详细信息。后续可接入节点编辑、AI 校验等交互。
          </p>
          <div
            ref={canvasRef}
            className={`fta-canvas-wrapper${
              exporting ? ' fta-canvas-exporting' : ''
            }`}
          >
            <FaultTreeCanvas
              graphData={graphData}
              onNodeSelect={setSelectedNode}
              canvasActionsRef={canvasActionsRef}
              showChrome
            />
          </div>

          {selectedNode && (
            <div className="fta-node-panel">
              <div className="fta-node-panel-header">
                <div>
                  <div className="fta-node-panel-title">
                    {selectedNode.data.label}
                  </div>
                  <div className="fta-node-panel-subtitle">
                    ID：{selectedNode.id}
                    {selectedMeta?.gateLabel && (
                      <> ｜ 逻辑门：{selectedMeta.gateLabel}</>
                    )}
                    {selectedNode.data.type && (
                      <> ｜ 类型：{selectedNode.data.type}</>
                    )}
                  </div>
                </div>
                <button
                  type="button"
                  className="fta-btn ghost"
                  onClick={() => setSelectedNode(null)}
                >
                  关闭
                </button>
              </div>

              {selectedMeta?.event && (
                <div className="fta-node-panel-body">
                  <div className="fta-node-panel-row">
                    <span className="fta-node-panel-label">事件编号</span>
                    <span className="fta-node-panel-value">
                      {selectedMeta.event.id || '—'}
                    </span>
                  </div>
                  <div className="fta-node-panel-row">
                    <span className="fta-node-panel-label">事件名称</span>
                    <span className="fta-node-panel-value">
                      {selectedMeta.event.name || '—'}
                    </span>
                  </div>
                  <div className="fta-node-panel-row">
                    <span className="fta-node-panel-label">描述</span>
                    <span className="fta-node-panel-value">
                      {selectedMeta.event.description || '—'}
                    </span>
                  </div>
                  <div className="fta-node-panel-row">
                    <span className="fta-node-panel-label">概率</span>
                    <span className="fta-node-panel-value">
                      {typeof selectedMeta.event.probability === 'number'
                        ? selectedMeta.event.probability
                        : '—'}
                    </span>
                  </div>
                  {Array.isArray(selectedMeta.event.rules) &&
                    selectedMeta.event.rules.length > 0 && (
                      <div className="fta-node-panel-rules">
                        <div className="fta-node-panel-label">触发规则</div>
                        <pre className="fta-node-panel-json">
                          {JSON.stringify(
                            selectedMeta.event.rules,
                            null,
                            2,
                          )}
                        </pre>
                      </div>
                    )}
                </div>
              )}
            </div>
          )}
        </section>
      </main>
    </div>
  )
}

export default FaultTreePage

