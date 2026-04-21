import { useCallback, useEffect, useState } from 'react'
import './exploded-3d-upload-modal.css'

function parsePartDetailsJson(text) {
  const raw = JSON.parse(text)
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('部件列表必须是 JSON 对象（Object_2 等为键）')
  }
  const out = {}
  for (const [k, v] of Object.entries(raw)) {
    if (typeof v === 'object' && v !== null && !Array.isArray(v)) {
      out[k] = {
        id: String(v.id ?? ''),
        name: String(v.name ?? ''),
        type: String(v.type ?? ''),
      }
    }
  }
  if (!Object.keys(out).length) {
    throw new Error('部件列表为空或格式不正确')
  }
  return out
}

export default function Exploded3dUploadModal({
  open,
  onClose,
  projectId,
  onSaved,
  onClear,
}) {
  const [glbFile, setGlbFile] = useState(null)
  const [jsonFile, setJsonFile] = useState(null)
  const [jsonPreview, setJsonPreview] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return undefined
    setError('')
    return undefined
  }, [open])

  const onPickGlb = (e) => {
    const f = e.target.files?.[0]
    setGlbFile(f || null)
    setError('')
    e.target.value = ''
  }

  const onPickJson = (e) => {
    const f = e.target.files?.[0]
    setJsonFile(f || null)
    setJsonPreview('')
    setError('')
    if (f) {
      const reader = new FileReader()
      reader.onload = () => {
        setJsonPreview(typeof reader.result === 'string' ? reader.result : '')
      }
      reader.onerror = () => setError('无法读取 JSON 文件')
      reader.readAsText(f, 'UTF-8')
    }
    e.target.value = ''
  }

  const handleSubmit = useCallback(async () => {
    setError('')
    if (!projectId) {
      setError('缺少项目 ID')
      return
    }
    if (!glbFile || !jsonFile) {
      setError('请同时选择 GLB 与部件列表 JSON')
      return
    }
    const lower = glbFile.name.toLowerCase()
    if (!lower.endsWith('.glb')) {
      setError('三维模型须为 .glb 文件')
      return
    }
    setBusy(true)
    try {
      const text = jsonPreview || (await jsonFile.text())
      const partDetails = parsePartDetailsJson(text)
      await onSaved?.({
        glbBlob: glbFile,
        partDetails,
      })
      setGlbFile(null)
      setJsonFile(null)
      setJsonPreview('')
      onClose?.()
    } catch (e) {
      setError(e?.message || String(e))
    } finally {
      setBusy(false)
    }
  }, [glbFile, jsonFile, jsonPreview, onClose, onSaved, projectId])

  useEffect(() => {
    if (!open) return undefined
    const onKey = (ev) => {
      if (ev.key === 'Escape') onClose?.()
    }
    document.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      className="e3d-modal-overlay"
      role="presentation"
      onClick={() => !busy && onClose?.()}
    >
      <div
        className="e3d-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="e3d-modal-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="e3d-modal-head">
          <h2 id="e3d-modal-title" className="e3d-modal-title">
            三维爆炸图与部件列表
          </h2>
          <button
            type="button"
            className="e3d-modal-close"
            aria-label="关闭"
            disabled={busy}
            onClick={() => onClose?.()}
          >
            ×
          </button>
        </header>
        <div className="e3d-modal-body">
          <p className="e3d-modal-desc">
            上传设备爆炸图（GLB）与部件列表（PART_DETAILS 格式的 JSON）。两者齐全时，故障树画布左侧将显示该
            GLB，并在 AI 生成故障树时把部件信息写入提示词，使事件可与物理组件对标。
          </p>
          <div className="e3d-field">
            <span className="e3d-label">1. 上传设备爆炸图（GLB）</span>
            <label className="e3d-file-btn">
              选择文件
              <input type="file" accept=".glb,model/gltf-binary" onChange={onPickGlb} disabled={busy} />
            </label>
            {glbFile ? (
              <span className="e3d-file-name" title={glbFile.name}>
                {glbFile.name}
              </span>
            ) : (
              <span className="e3d-file-placeholder">未选择</span>
            )}
          </div>
          <div className="e3d-field">
            <span className="e3d-label">2. 上传部件列表（PART_DETAILS，JSON）</span>
            <label className="e3d-file-btn">
              选择文件
              <input type="file" accept=".json,application/json" onChange={onPickJson} disabled={busy} />
            </label>
            {jsonFile ? (
              <span className="e3d-file-name" title={jsonFile.name}>
                {jsonFile.name}
              </span>
            ) : (
              <span className="e3d-file-placeholder">未选择</span>
            )}
          </div>
          {error ? <p className="e3d-error">{error}</p> : null}
        </div>
        <footer className="e3d-modal-foot">
          <button
            type="button"
            className="e3d-btn e3d-btn--ghost"
            disabled={busy}
            title="清除本地已保存的 GLB 与部件列表"
            onClick={async () => {
              setError('')
              setBusy(true)
              try {
                await onClear?.()
                setGlbFile(null)
                setJsonFile(null)
                setJsonPreview('')
                onClose?.()
              } catch (e) {
                setError(e?.message || String(e))
              } finally {
                setBusy(false)
              }
            }}
          >
            清除三维资料
          </button>
          <div className="e3d-modal-foot-actions">
            <button type="button" className="e3d-btn e3d-btn--ghost" disabled={busy} onClick={() => onClose?.()}>
              取消
            </button>
            <button type="button" className="e3d-btn e3d-btn--primary" disabled={busy} onClick={handleSubmit}>
              {busy ? '保存中…' : '保存'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}
