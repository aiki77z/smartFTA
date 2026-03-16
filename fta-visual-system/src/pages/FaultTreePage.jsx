import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import FaultTreeCanvas from '../components/fta/FaultTreeCanvas.jsx'
import rawFtaSample from '../raw-FTA/raw-FTA.json'
import { parseRawFtaJson } from '../utils/ftaParser.js'
import {
  addChildNode,
  deleteNode,
  deleteGate,
  changeGateType,
  renameNode,
  changeEventType,
  insertGate,
  graphToRawJson,
  getNodeEditInfo,
} from '../utils/ftaGraphEditor.js'
import '../styles/fta.css'

const TYPE_LABELS = { top: '顶事件', intermediate: '中间事件', basic: '基本事件' }
const EVENT_TYPES = ['top', 'intermediate', 'basic']

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
  const [contextMenu, setContextMenu] = useState(null)
  const [modal, setModal] = useState(null)
  const canvasRef = useRef(null)
  const canvasActionsRef = useRef(null)

  useEffect(() => {
    if (!contextMenu) return
    const dismiss = () => setContextMenu(null)
    const timer = setTimeout(() => document.addEventListener('click', dismiss), 0)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('click', dismiss)
    }
  }, [contextMenu])

  const rawAttr = useMemo(() => {
    try {
      const parsed = JSON.parse(rawJsonText)
      return parsed.attr
    } catch {
      return undefined
    }
  }, [rawJsonText])

  const applyEdit = useCallback(
    (newGraphData) => {
      setGraphData(newGraphData)
      setRawJsonText(JSON.stringify(graphToRawJson(newGraphData, rawAttr), null, 2))
      setError('')
    },
    [rawAttr],
  )

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
      await new Promise((r) => setTimeout(r, 50))
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

  const handleNodeContextMenu = useCallback(
    (event, rfNode) => {
      const info = getNodeEditInfo(graphData, rfNode.id)
      if (!info) return
      setContextMenu({
        x: event.clientX,
        y: event.clientY,
        info,
      })
    },
    [graphData],
  )

  const handlePaneContextMenu = useCallback(() => {
    setContextMenu(null)
  }, [])

  const handleNodeDoubleClick = useCallback(
    (_event, rfNode) => {
      const node = graphData.nodes.find((n) => n.id === rfNode.id)
      if (!node || node.type === 'gate') return
      setModal({ type: 'rename', nodeId: node.id, value: node.label })
    },
    [graphData],
  )

  const doAddChild = useCallback(
    (parentId, name, type) => {
      applyEdit(addChildNode(graphData, parentId, name, type))
      setSelectedNode(null)
    },
    [graphData, applyEdit],
  )

  const doDelete = useCallback(
    (nodeId) => {
      applyEdit(deleteNode(graphData, nodeId))
      setSelectedNode(null)
    },
    [graphData, applyEdit],
  )

  const doDeleteGate = useCallback(
    (gateId) => {
      applyEdit(deleteGate(graphData, gateId))
      setSelectedNode(null)
    },
    [graphData, applyEdit],
  )

  const doChangeGate = useCallback(
    (gateId, newType) => {
      applyEdit(changeGateType(graphData, gateId, newType))
    },
    [graphData, applyEdit],
  )

  const doRename = useCallback(
    (nodeId, newName) => {
      applyEdit(renameNode(graphData, nodeId, newName))
      const sel = selectedNode
      if (sel && sel.id === nodeId) {
        setSelectedNode({ ...sel, data: { ...sel.data, label: newName } })
      }
    },
    [graphData, applyEdit, selectedNode],
  )

  const doChangeType = useCallback(
    (nodeId, newType) => {
      applyEdit(changeEventType(graphData, nodeId, newType))
    },
    [graphData, applyEdit],
  )

  const doInsertGate = useCallback(
    (parentId, gateType) => {
      applyEdit(insertGate(graphData, parentId, gateType))
    },
    [graphData, applyEdit],
  )

  const hasGraph = useMemo(
    () => graphData.nodes.length > 0,
    [graphData.nodes.length],
  )

  const selectedMeta = selectedNode?.data?.meta

  function renderContextMenu() {
    if (!contextMenu) return null
    const { x, y, info } = contextMenu
    const { node, isGate, gateChild, hasDirectChildren, isTop } = info

    const menuStyle = {
      left: Math.min(x, window.innerWidth - 220),
      top: Math.min(y, window.innerHeight - 320),
    }

    if (isGate) {
      const otherType = node.label === 'AND' ? 'OR' : 'AND'
      return (
        <div className="fta-context-menu" style={menuStyle}>
          <button
            className="fta-context-menu-item"
            onClick={() => {
              doChangeGate(node.id, otherType)
              setContextMenu(null)
            }}
          >
            <span className="fta-ctx-icon">⇄</span>
            切换为 {otherType} 门
          </button>
          <div className="fta-context-menu-sep" />
          <button
            className="fta-context-menu-item fta-context-menu-item--danger"
            onClick={() => {
              doDeleteGate(node.id)
              setContextMenu(null)
            }}
          >
            <span className="fta-ctx-icon">✕</span>
            删除逻辑门
          </button>
        </div>
      )
    }

    return (
      <div className="fta-context-menu" style={menuStyle}>
        <button
          className="fta-context-menu-item"
          onClick={() => {
            setContextMenu(null)
            setModal({ type: 'addChild', parentId: node.id })
          }}
        >
          <span className="fta-ctx-icon">＋</span>
          添加子节点
        </button>
        <button
          className="fta-context-menu-item"
          onClick={() => {
            setContextMenu(null)
            setModal({ type: 'rename', nodeId: node.id, value: node.label })
          }}
        >
          <span className="fta-ctx-icon">✎</span>
          修改名称
        </button>
        <div className="fta-context-menu-sep" />
        <div className="fta-context-menu-label">事件类型</div>
        {EVENT_TYPES.map((t) => (
          <button
            key={t}
            className={`fta-context-menu-item fta-context-menu-item--type${
              node.type === t ? ' fta-context-menu-item--active' : ''
            }`}
            disabled={node.type === t}
            onClick={() => {
              doChangeType(node.id, t)
              setContextMenu(null)
            }}
          >
            <span
              className={`fta-ctx-type-dot fta-ctx-type-dot--${t}`}
            />
            {TYPE_LABELS[t]}
          </button>
        ))}

        {gateChild && (
          <>
            <div className="fta-context-menu-sep" />
            <div className="fta-context-menu-label">逻辑门</div>
            <button
              className="fta-context-menu-item"
              onClick={() => {
                doChangeGate(
                  gateChild.id,
                  gateChild.label === 'AND' ? 'OR' : 'AND',
                )
                setContextMenu(null)
              }}
            >
              <span className="fta-ctx-icon">⇄</span>
              切换为 {gateChild.label === 'AND' ? 'OR' : 'AND'} 门
            </button>
          </>
        )}

        {!gateChild && hasDirectChildren && (
          <>
            <div className="fta-context-menu-sep" />
            <div className="fta-context-menu-label">插入逻辑门</div>
            <button
              className="fta-context-menu-item"
              onClick={() => {
                doInsertGate(node.id, 'AND')
                setContextMenu(null)
              }}
            >
              <span className="fta-ctx-icon">⊓</span>
              插入 AND 门
            </button>
            <button
              className="fta-context-menu-item"
              onClick={() => {
                doInsertGate(node.id, 'OR')
                setContextMenu(null)
              }}
            >
              <span className="fta-ctx-icon">⊔</span>
              插入 OR 门
            </button>
          </>
        )}

        {!isTop && (
          <>
            <div className="fta-context-menu-sep" />
            <button
              className="fta-context-menu-item fta-context-menu-item--danger"
              onClick={() => {
                doDelete(node.id)
                setContextMenu(null)
              }}
            >
              <span className="fta-ctx-icon">✕</span>
              删除节点
            </button>
          </>
        )}
      </div>
    )
  }

  function renderModal() {
    if (!modal) return null

    if (modal.type === 'addChild') {
      return (
        <AddChildModal
          onConfirm={(name, type) => {
            doAddChild(modal.parentId, name, type)
            setModal(null)
          }}
          onCancel={() => setModal(null)}
        />
      )
    }

    if (modal.type === 'rename') {
      return (
        <RenameModal
          initialValue={modal.value}
          onConfirm={(name) => {
            doRename(modal.nodeId, name)
            setModal(null)
          }}
          onCancel={() => setModal(null)}
        />
      )
    }

    return null
  }

  return (
    <div className="fta-layout">
      <header className="fta-header">
        <div>
          <h1 className="fta-title">故障树可视化编辑</h1>
          <p className="fta-subtitle">
            右键节点可编辑 · 双击修改名称 · 操作自动同步 JSON
          </p>
        </div>
        <div className="fta-header-actions">
          <button type="button" className="fta-btn ghost" onClick={handleDownloadJson}>
            下载 JSON
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
          <h2 className="fta-section-title">故障树 JSON</h2>
          <p className="fta-section-desc">
            画布编辑自动同步此处。也可粘贴 JSON 后点击"应用到画布"。
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
          <div className="fta-canvas-toolbar">
            <h2 className="fta-section-title" style={{ margin: 0 }}>
              故障树画布
            </h2>
            <span className="fta-toolbar-hint">
              右键节点编辑 · 双击修改名称 · 拖拽平移 · 滚轮缩放
            </span>
          </div>
          <div
            ref={canvasRef}
            className={`fta-canvas-wrapper${exporting ? ' fta-canvas-exporting' : ''}`}
          >
            <FaultTreeCanvas
              graphData={graphData}
              onNodeSelect={setSelectedNode}
              onNodeContextMenu={handleNodeContextMenu}
              onPaneContextMenu={handlePaneContextMenu}
              onNodeDoubleClick={handleNodeDoubleClick}
              canvasActionsRef={canvasActionsRef}
              showChrome
            />
          </div>

          {selectedNode && (
            <NodeInfoPanel
              selectedNode={selectedNode}
              selectedMeta={selectedMeta}
              onClose={() => setSelectedNode(null)}
              onRename={(newName) => doRename(selectedNode.id, newName)}
              onChangeType={(newType) => doChangeType(selectedNode.id, newType)}
              onDelete={() => doDelete(selectedNode.id)}
            />
          )}
        </section>
      </main>

      {renderContextMenu()}
      {renderModal()}
    </div>
  )
}

function NodeInfoPanel({
  selectedNode,
  selectedMeta,
  onClose,
  onRename,
  onChangeType,
  onDelete,
}) {
  const [editingName, setEditingName] = useState(false)
  const [nameValue, setNameValue] = useState(selectedNode.data.label)
  const isGate = selectedNode.data.type === 'gate'
  const isTop = selectedNode.data.type === 'top'

  useEffect(() => {
    setNameValue(selectedNode.data.label)
    setEditingName(false)
  }, [selectedNode.id, selectedNode.data.label])

  return (
    <div className="fta-node-panel">
      <div className="fta-node-panel-header">
        <div style={{ flex: 1 }}>
          {editingName ? (
            <div className="fta-inline-edit">
              <input
                className="fta-inline-edit-input"
                value={nameValue}
                onChange={(e) => setNameValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    onRename(nameValue)
                    setEditingName(false)
                  }
                  if (e.key === 'Escape') setEditingName(false)
                }}
                autoFocus
              />
              <button
                className="fta-btn-xs primary"
                onClick={() => {
                  onRename(nameValue)
                  setEditingName(false)
                }}
              >
                ✓
              </button>
              <button
                className="fta-btn-xs ghost"
                onClick={() => setEditingName(false)}
              >
                ✕
              </button>
            </div>
          ) : (
            <div className="fta-node-panel-title">
              {selectedNode.data.label}
              {!isGate && (
                <button
                  className="fta-btn-icon"
                  title="修改名称"
                  onClick={() => setEditingName(true)}
                >
                  ✎
                </button>
              )}
            </div>
          )}
          <div className="fta-node-panel-subtitle">
            ID：{selectedNode.id}
            {selectedNode.data.type && (
              <> · 类型：{TYPE_LABELS[selectedNode.data.type] || selectedNode.data.type}</>
            )}
          </div>
        </div>
        <button type="button" className="fta-btn ghost" onClick={onClose}>
          关闭
        </button>
      </div>

      {!isGate && (
        <div className="fta-node-panel-type-bar">
          {EVENT_TYPES.map((t) => (
            <button
              key={t}
              className={`fta-type-pill${selectedNode.data.type === t ? ' fta-type-pill--active' : ''}`}
              onClick={() => onChangeType(t)}
              disabled={selectedNode.data.type === t}
            >
              {TYPE_LABELS[t]}
            </button>
          ))}
        </div>
      )}

      {selectedMeta?.event && (
        <div className="fta-node-panel-body">
          <InfoRow label="事件编号" value={selectedMeta.event.id} />
          <InfoRow label="事件名称" value={selectedMeta.event.name} />
          <InfoRow label="描述" value={selectedMeta.event.description} />
          <InfoRow label="错误等级" value={selectedMeta.event.errorLevel} />
          <InfoRow
            label="概率"
            value={
              typeof selectedMeta.event.probability === 'number'
                ? selectedMeta.event.probability
                : undefined
            }
          />
          <InfoRow label="排查方法" value={selectedMeta.event.investigateMethod} />
          {Array.isArray(selectedMeta.event.documents) &&
            selectedMeta.event.documents.length > 0 && (
              <div className="fta-node-panel-docs">
                <div className="fta-node-panel-label">文档溯源</div>
                {selectedMeta.event.documents.map((doc, i) => (
                  <div key={i} className="fta-doc-tag">
                    {doc}
                  </div>
                ))}
              </div>
            )}
          {Array.isArray(selectedMeta.event.rules) &&
            selectedMeta.event.rules.length > 0 && (
              <div className="fta-node-panel-rules">
                <div className="fta-node-panel-label">触发规则</div>
                <pre className="fta-node-panel-json">
                  {JSON.stringify(selectedMeta.event.rules, null, 2)}
                </pre>
              </div>
            )}
        </div>
      )}

      {!isGate && !isTop && (
        <button
          className="fta-btn full ghost fta-btn-delete"
          onClick={onDelete}
        >
          删除此节点
        </button>
      )}
    </div>
  )
}

function InfoRow({ label, value }) {
  if (value === undefined || value === null || value === '') return null
  return (
    <div className="fta-node-panel-row">
      <span className="fta-node-panel-label">{label}</span>
      <span className="fta-node-panel-value">{String(value)}</span>
    </div>
  )
}

function AddChildModal({ onConfirm, onCancel }) {
  const [name, setName] = useState('')
  const [type, setType] = useState('basic')
  const inputRef = useRef(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const handleSubmit = () => {
    const trimmed = name.trim()
    if (!trimmed) return
    onConfirm(trimmed, type)
  }

  return (
    <div className="fta-modal-overlay" onClick={onCancel}>
      <div className="fta-modal" onClick={(e) => e.stopPropagation()}>
        <div className="fta-modal-title">添加子节点</div>
        <label className="fta-modal-label">事件名称</label>
        <input
          ref={inputRef}
          className="fta-modal-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
          placeholder="请输入事件名称"
        />
        <label className="fta-modal-label" style={{ marginTop: '0.8rem' }}>
          事件类型
        </label>
        <div className="fta-modal-types">
          {['basic', 'intermediate'].map((t) => (
            <button
              key={t}
              className={`fta-type-pill${type === t ? ' fta-type-pill--active' : ''}`}
              onClick={() => setType(t)}
            >
              {TYPE_LABELS[t]}
            </button>
          ))}
        </div>
        <div className="fta-modal-actions">
          <button className="fta-btn ghost" onClick={onCancel}>
            取消
          </button>
          <button
            className="fta-btn primary"
            onClick={handleSubmit}
            disabled={!name.trim()}
          >
            确认添加
          </button>
        </div>
      </div>
    </div>
  )
}

function RenameModal({ initialValue, onConfirm, onCancel }) {
  const [name, setName] = useState(initialValue)
  const inputRef = useRef(null)

  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [])

  const handleSubmit = () => {
    const trimmed = name.trim()
    if (!trimmed) return
    onConfirm(trimmed)
  }

  return (
    <div className="fta-modal-overlay" onClick={onCancel}>
      <div className="fta-modal" onClick={(e) => e.stopPropagation()}>
        <div className="fta-modal-title">修改名称</div>
        <input
          ref={inputRef}
          className="fta-modal-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
        />
        <div className="fta-modal-actions">
          <button className="fta-btn ghost" onClick={onCancel}>
            取消
          </button>
          <button
            className="fta-btn primary"
            onClick={handleSubmit}
            disabled={!name.trim()}
          >
            确认
          </button>
        </div>
      </div>
    </div>
  )
}

export default FaultTreePage
