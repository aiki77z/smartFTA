import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  generateAllTrees,
  getTree,
  getTreeVersion,
  pollBatchJob,
} from '../api/ftaBackend.js'
import {
  downloadKbJobUploadedFile,
  importMaintenanceCases,
  importWorkOrders,
  listAllChunks,
  pollKbJob,
  profileKnowledgeFile,
  startKbJobUpload,
} from '../api/kbBackend.js'
import KnowledgeBasePanel from '../components/KnowledgeBasePanel.jsx'
import KnowledgeGraphModal from '../components/KnowledgeGraphModal.jsx'
import KnowledgeImportModal from '../components/KnowledgeImportModal.jsx'
import Exploded3dUploadModal from '../components/Exploded3dUploadModal.jsx'
import {
  chunkBelongsToKbFile,
  getChunkBodyText,
  matchChunkToKbFile,
} from '../components/knowledgeBaseConstants.js'
import {
  DEFAULT_WORKSPACE_MESSAGES,
  ensureProject,
  getProjectById,
  getWorkspace,
  renameProject,
  renameProjectFromFirstFile,
  saveWorkspace,
} from '../utils/projectStore.js'
import {
  inferCanvasDisplayTitle,
  listCanvasDrafts,
  removeCanvasDraft,
  upsertCanvasDraft,
} from '../utils/canvasDraftStore.js'
import {
  deleteExploded3dBundle,
  loadExploded3dBundle,
  saveExploded3dBundle,
} from '../utils/exploded3dProjectStore.js'
import { buildFaultTreeThumbnailScene } from '../utils/faultTreeThumbnailLayout.js'
import { normalizeBackendTreeDocumentToGraph } from '../utils/ftaParser.js'
import { IconChevronLeft } from '../components/icons.jsx'
import ThemeToggle from '../components/ThemeToggle.jsx'
import '../styles/home.css'

const DEFAULT_FTA_BASE_URL = 'http://localhost:8000'

/**
 * 知识库构建全流程成功后的本地持久标记（随 workspace.files 写入 localStorage）。
 * KB 后端重启后仍可显示「解析成功」，且恢复轮询时不会因接口失败被误判为解析失败。
 */
function migrateKbFileFromWorkspace(file) {
  if (!file || typeof file !== 'object') return file
  if (file.kbImportComplete) return file
  if (file.status === 'done') {
    return { ...file, kbImportComplete: true }
  }
  return file
}

function withKbImportCompleteIfDone(file, job) {
  if (!file || typeof file !== 'object') return file
  if (file.kbImportComplete) return file
  const st = String(job?.status || '').toLowerCase()
  if (st === 'failed' || st === 'error') return file
  const done =
    st === 'success' ||
    st === 'completed' ||
    st === 'finished' ||
    st === 'done' ||
    st === 'succeeded' ||
    st === 'successful' ||
    st === 'completed_with_sync_error'
  if (!done) return file
  return { ...file, kbImportComplete: true }
}

function lastChatTimestampFromMessages(messages) {
  if (!Array.isArray(messages)) return null
  let max = 0
  for (const m of messages) {
    const t = Number(m?.at)
    if (Number.isFinite(t) && t > max) max = t
  }
  return max > 0 ? max : null
}

function formatBatchProgressInline(job, running) {
  if (!job || typeof job !== 'object') return running ? '正在提交…' : ''
  const total = Number(job.total)
  const success = Number(job.success)
  const failed = Number(job.failed)
  const st = String(job.status || '').toLowerCase()
  const t = Number.isFinite(total) ? total : null
  const s = Number.isFinite(success) ? success : 0
  const f = Number.isFinite(failed) ? failed : 0
  const done = s + f
  if (t != null && t > 0) {
    return `${done}/${t} 棵 · ${s} 成功 · ${f} 失败`
  }
  if (running || st === 'pending' || st === 'running') {
    return String(job.message || '').trim() || '批量任务进行中…'
  }
  return String(job.message || '').trim() || '—'
}

function formatFaultTreeChatTime(ts) {
  if (!ts || !Number.isFinite(Number(ts))) return '暂无对话'
  const d = new Date(Number(ts))
  if (Number.isNaN(d.getTime())) return '暂无对话'
  return d.toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'short' })
}

function kbCategoryLabel(category) {
  switch (String(category || '').trim()) {
    case 'document':
      return '文档资料'
    case 'work_order':
      return '工单数据'
    case 'maintenance_record':
      return '维修记录'
    default:
      return ''
  }
}

function inferSourceTypeFromCategory(category) {
  switch (String(category || '').trim()) {
    case 'document':
      return 'manual_document'
    case 'work_order':
      return 'work_order'
    case 'maintenance_record':
      return 'maintenance_record'
    default:
      return ''
  }
}

function MiniFaultTreeThumbnail({ graphData }) {
  const scene = useMemo(() => buildFaultTreeThumbnailScene(graphData), [graphData])
  if (!scene?.shapes?.length) {
    return <div className="home-ft-thumb-placeholder">空白</div>
  }
  return (
    <svg className="home-ft-thumb-svg" viewBox={scene.viewBox} aria-hidden>
      {scene.lines.map((ln, i) => (
        <line
          key={`ln-${i}`}
          x1={ln.x1}
          y1={ln.y1}
          x2={ln.x2}
          y2={ln.y2}
          stroke="currentColor"
          strokeWidth="1.35"
          opacity={0.38}
        />
      ))}
      {scene.shapes.map((s) => {
        if (s.kind === 'ellipse') {
          const stroke = s.nodeType === 'basic' ? '#8dc08a' : '#64748b'
          const fill = s.nodeType === 'basic' ? '#DEFACD' : '#e2e8f0'
          return (
            <ellipse
              key={s.id}
              cx={s.cx}
              cy={s.cy}
              rx={s.rx}
              ry={s.ry}
              fill={fill}
              stroke={stroke}
              strokeWidth="1.25"
            />
          )
        }
        if (s.kind === 'gate') {
          const stroke = s.isAnd ? '#f59e0b' : '#a855f7'
          const fill = s.isAnd ? '#fffbeb' : '#faf5ff'
          return (
            <rect
              key={s.id}
              x={s.x}
              y={s.y}
              width={s.w}
              height={s.h}
              rx={Math.min(3, s.w * 0.12)}
              fill={fill}
              stroke={stroke}
              strokeWidth="1.25"
            />
          )
        }
        const stroke =
          s.nodeType === 'top' ? '#4a62c4' : s.nodeType === 'intermediate' ? '#d4708f' : '#64748b'
        const fill =
          s.nodeType === 'top' ? '#CEEBFF' : s.nodeType === 'intermediate' ? '#FFF0FF' : '#f1f5f9'
        return (
          <rect
            key={s.id}
            x={s.x}
            y={s.y}
            width={s.w}
            height={s.h}
            rx={s.rx}
            fill={fill}
            stroke={stroke}
            strokeWidth="1.25"
          />
        )
      })}
    </svg>
  )
}

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
  const [remoteFileObject, setRemoteFileObject] = useState(null)
  const isModal = variant === 'modal'

  const effectiveFileObject = fileObject || remoteFileObject

  const kind = useMemo(
    () => getPreviewKind(fileMeta?.name, effectiveFileObject?.type),
    [fileMeta?.name, effectiveFileObject?.type],
  )

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    setTextContent('')
    setTextError('')
    setTextLoading(false)
    setPdfObjectUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev)
      return ''
    })

    if (!fileMeta) return undefined

    // 若本地 File 丢失（例如离开页面回来），尝试从 KB 后端按 kbJobId 拉取原文件 blob 以供预览
    if (!fileObject && fileMeta?.kbJobId && !remoteFileObject) {
      const ac = new AbortController()
      downloadKbJobUploadedFile({ jobId: fileMeta.kbJobId, signal: ac.signal })
        .then((blob) => {
          const b = blob instanceof Blob ? blob : null
          if (!b) return
          const name = fileMeta?.name || 'upload.bin'
          const f = new File([b], name, { type: b.type || '' })
          setRemoteFileObject(f)
        })
        .catch(() => {
          // ignore
        })
      return () => ac.abort()
    }

    if (!effectiveFileObject) return undefined

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
      reader.readAsText(effectiveFileObject, 'UTF-8')
      return undefined
    }

    if (kind === 'pdf') {
      const url = URL.createObjectURL(effectiveFileObject)
      setPdfObjectUrl(url)
      return () => {
        URL.revokeObjectURL(url)
      }
    }

    return undefined
  }, [fileMeta?.id, fileMeta?.kbJobId, fileObject, remoteFileObject, kind])
  /* eslint-enable react-hooks/set-state-in-effect */

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

  if (!effectiveFileObject) {
    return (
      <div className={phCls}>
        该文件仅有记录（例如刷新页面后从本地恢复），正在尝试从 KB 后端恢复原文用于预览…
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

function HomePage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { projectId = '' } = useParams()
  const [files, setFiles] = useState([])
  const [selectedFileId, setSelectedFileId] = useState(null)
  /** 与右侧「故障树」卡片联动：高亮画布内已选知识库文件 */
  const [linkedKbFileIds, setLinkedKbFileIds] = useState([])
  const [activeFaultTreeKey, setActiveFaultTreeKey] = useState(null)
  const [faultTreeRefreshKey, setFaultTreeRefreshKey] = useState(0)
  const [projectName, setProjectName] = useState('')
  const [editingProjectName, setEditingProjectName] = useState(false)
  const [workspaceReady, setWorkspaceReady] = useState(false)
  const [previewModalOpen, setPreviewModalOpen] = useState(false)
  const [previewFileObject, setPreviewFileObject] = useState(null)
  const [importModalOpen, setImportModalOpen] = useState(false)
  const [pendingImportItems, setPendingImportItems] = useState([])
  const [importSubmitting, setImportSubmitting] = useState(false)
  /** 知识库构建：对接 FTA-KB job（GET /api/kb/jobs/{job_id}） */
  const [kbJob, setKbJob] = useState(null)
  const [kbPrimaryFileId, setKbPrimaryFileId] = useState('')
  const kbPrimaryFileIdRef = useRef('')
  const [kbHydrating, setKbHydrating] = useState(false)
  /** 记录每个文件对应的 KB job_id（用于多文件并行轮询与显示解析进度） */
  const kbJobIdByFileIdRef = useRef(new Map())
  const kbPollersByFileIdRef = useRef(new Map())
  /** 勾选参与分块预览的文件（默认勾选新上传文件）；与「本轮知识库来源」一致 */
  const [kbChunkIncludeById, setKbChunkIncludeById] = useState({})
  /** 项目页知识库来源/文件集合变化代数，供画布判断下一条是否须走 FTA generate */
  const [kbDatasetEpoch, setKbDatasetEpoch] = useState(0)
  const [kbChunks, setKbChunks] = useState([])
  const [kbChunksLoading, setKbChunksLoading] = useState(false)
  const [kbChunksError, setKbChunksError] = useState('')
  const [kbChunksPanelOpen, setKbChunksPanelOpen] = useState(true)
  /** 批量生成故障树（FTA-GNR /api/batch/generate-all） */
  const [batchJob, setBatchJob] = useState(null)
  const [batchJobError, setBatchJobError] = useState('')
  const [batchJobRunning, setBatchJobRunning] = useState(false)
  const batchPollerRef = useRef(null)
  /** 批量生成：已成功写入本地画布草稿的 job_item，避免轮询重复导入 */
  const batchImportedItemIdsRef = useRef(new Set())
  /** 批量生成开始时快照的「知识库来源文件 id」，写入画布草稿供概览高亮 */
  const batchKbSourceFileIdsRef = useRef([])
  const skipTitleBlurRef = useRef(false)
  const kbPollerRef = useRef(null)
  const [graphModalOpen, setGraphModalOpen] = useState(false)
  const [threeDModalOpen, setThreeDModalOpen] = useState(false)
  /** 当前项目是否在 IndexedDB 中存有 GLB+部件 JSON（与生成故障树提示词联动） */
  const [exploded3dConfigured, setExploded3dConfigured] = useState(false)
  /** 仅内存：上传的 File 对象，用于本地预览；不写入 localStorage */
  const fileObjectStoreRef = useRef(new Map())

  const openGraphModal = useCallback(() => {
    setGraphModalOpen(true)
  }, [])

  const closeGraphModal = useCallback(() => {
    setGraphModalOpen(false)
  }, [])

  const openThreeDModal = useCallback(() => {
    setThreeDModalOpen(true)
  }, [])

  const closeThreeDModal = useCallback(() => {
    setThreeDModalOpen(false)
  }, [])

  const kbProgressFromJob = useCallback((job) => {
    const jp = Number(job?.progress)
    if (Number.isFinite(jp) && jp >= 0) return Math.max(0, Math.min(100, jp))
    const st = String(job?.status || '').toLowerCase()
    if (st === 'queued') return 1
    if (st === 'running') return 35
    if (st === 'syncing') return 85
    if (st === 'success' || st === 'completed' || st === 'finished') return 100
    if (st === 'completed_with_sync_error') return 100
    if (st === 'failed') return 100
    return 10
  }, [])

  const kbStatusFromJob = useCallback((job) => {
    const st = String(job?.status || '').toLowerCase()
    if (
      st === 'success' ||
      st === 'completed' ||
      st === 'finished' ||
      st === 'done' ||
      st === 'succeeded' ||
      st === 'successful'
    ) {
      return 'done'
    }
    if (st === 'completed_with_sync_error') return 'failed'
    if (st === 'failed' || st === 'error') return 'failed'
    return 'processing'
  }, [])

  const updateKbFileAfterDirectImport = useCallback((fileId, payload, category) => {
    const fileMeta = payload?.file || {}
    const counts = payload?.imported || payload?.counts || {}
    const logicalFileId = fileMeta?.file_id || ''
    setFiles((prevFiles) => {
      const relatedIds = prevFiles.filter((f) => logicalFileId && f.fileId === logicalFileId).map((f) => f.id)
      setKbChunkIncludeById((prev) => {
        const next = { ...prev }
        relatedIds.forEach((id) => { next[id] = id === fileId })
        next[fileId] = true
        return next
      })
      return prevFiles.map((f) => {
        if (f.id !== fileId) {
          return logicalFileId && f.fileId === logicalFileId ? { ...f, isActive: false } : f
        }
        return {
          ...f,
          status: 'done',
          kbImportComplete: true,
          parseProgress: 100,
          kbCategory: category,
          kbCategoryLabel: kbCategoryLabel(category),
          sourceType: payload?.source_type || inferSourceTypeFromCategory(category),
          fileVersionId: fileMeta?.file_version_id || f.fileVersionId || '',
          fileId: logicalFileId || f.fileId || '',
          versionNo: fileMeta?.version_no || f.versionNo || null,
          isActive: true,
          importedFileName: fileMeta?.file_name || f.importedFileName || '',
          resultSummary: counts,
        }
      })
    })
  }, [])

  const markKbFileImportFailed = useCallback((fileId, category, error) => {
    setFiles((prevFiles) =>
      prevFiles.map((f) => {
        if (f.id !== fileId) return f
        return {
          ...f,
          status: 'failed',
          kbCategory: category,
          kbCategoryLabel: kbCategoryLabel(category),
          sourceType: inferSourceTypeFromCategory(category),
          error: error?.message || String(error || '导入失败'),
        }
      }),
    )
  }, [])

  const startKbPollingForFile = useCallback(
    (fileId, jobId) => {
      if (!fileId || !jobId) return
      const prev = kbPollersByFileIdRef.current.get(fileId)
      if (prev) prev.abort?.()
      const controller = new AbortController()
      kbPollersByFileIdRef.current.set(fileId, controller)

      void pollKbJob({
        jobId,
        signal: controller.signal,
        onUpdate: (job) => {
          // 左栏上方只展示“主文件”的 job，避免多个轮询互相覆盖造成错误闪烁
          const primary = kbPrimaryFileIdRef.current
          if (!primary || primary === fileId) {
            setKbJob(job)
            if (kbHydrating) setKbHydrating(false)
          }
          const pct = kbProgressFromJob(job)
          const importedFile = job?.sync_response?.file
          const importedFileVersionId = importedFile?.file_version_id || job?.file_version_id || ''
          const importedFileId = importedFile?.file_id || job?.file_id || ''
          const importedFileName = importedFile?.file_name || job?.uploaded_file_name || ''
          const terminalStatus = kbStatusFromJob(job)
          setFiles((prevFiles) => {
            const relatedIds = prevFiles.filter((f) => importedFileId && f.fileId === importedFileId).map((f) => f.id)
            if (terminalStatus === 'done') {
              setKbChunkIncludeById((prev) => {
                const next = { ...prev }
                relatedIds.forEach((id) => { next[id] = id === fileId })
                next[fileId] = true
                return next
              })
            }
            return prevFiles.map((f) => {
              if (f.id !== fileId) {
                return terminalStatus === 'done' && importedFileId && f.fileId === importedFileId
                  ? { ...f, isActive: false }
                  : f
              }
              const next = {
                ...f,
                kbJobId: jobId,
                fileVersionId: importedFileVersionId || f.fileVersionId || '',
                fileId: importedFileId || f.fileId || '',
                importedFileName: importedFileName || f.importedFileName || '',
                versionNo: importedFile?.version_no || job?.version_no || f.versionNo || null,
                isActive: terminalStatus === 'done' ? true : f.isActive,
                parseProgress: Math.max(f.parseProgress || 0, pct),
                status: kbStatusFromJob(job),
              }
              return withKbImportCompleteIfDone(next, job)
            })
          })
        },
        intervalMs: 2000,
      }).catch((e) => {
        if (e?.name === 'AbortError') return
        // KB 不可达时：已本地标记「构建完成」的文件保持成功，不覆盖为失败
        setFiles((prevFiles) =>
          prevFiles.map((f) => {
            if (f.id !== fileId) return f
            if (f.kbImportComplete || f.status === 'done') return f
            return { ...f, status: 'failed' }
          }),
        )
      })
    },
    [kbProgressFromJob, kbStatusFromJob, kbHydrating],
  )

  const profileWorkOrderForModal = useCallback(async (file) => {
    return await profileKnowledgeFile({ file, outputDir: './output' })
  }, [])

  const startDocumentImportForFile = useCallback(
    (meta, file, plan) => {
      const generateFtaBaseUrl = import.meta?.env?.VITE_FTA_BACKEND_URL || DEFAULT_FTA_BASE_URL
      setKbPrimaryFileId(meta.id)
      setKbJob({
        job_id: '',
        status: 'queued',
        stage: 'queued',
        progress: 0,
        message: `正在提交文档到 KB 后端…（${file.name}）`,
        uploaded_file: { name: file.name },
      })
      startKbJobUpload({
        file,
        outputDir: './output',
        chunkSize: Number(plan?.documentConfig?.chunkSize) || 800,
        skipEntity: Boolean(plan?.documentConfig?.skipEntity),
        skipRelation: Boolean(plan?.documentConfig?.skipRelation),
        syncToGenerateFta: plan?.documentConfig?.syncToGenerateFta !== false,
        generateFtaBaseUrl,
        clearGraphBeforeImport: false,
      })
        .then((resp) => {
          const jobId = resp?.job_id || resp?.jobId || ''
          if (jobId) kbJobIdByFileIdRef.current.set(meta.id, jobId)
          setFiles((prevFiles) =>
            prevFiles.map((f) =>
              f.id === meta.id
                ? {
                    ...f,
                    kbJobId: jobId,
                    kbCategory: 'document',
                    kbCategoryLabel: kbCategoryLabel('document'),
                    sourceType: 'manual_document',
                  }
                : f,
            ),
          )
          setKbJob((prev) => (prev && typeof prev === 'object' ? { ...prev, ...resp } : resp))
          startKbPollingForFile(meta.id, jobId)
        })
        .catch((e) => {
          setKbJob({
            job_id: '',
            status: 'failed',
            stage: 'failed',
            progress: 0,
            message: '提交 KB 任务失败',
            error: e?.message || '提交 KB 任务失败',
          })
          markKbFileImportFailed(meta.id, 'document', e)
        })
    },
    [markKbFileImportFailed, startKbPollingForFile],
  )

  const startWorkOrderImportForFile = useCallback(
    (meta, file, plan) => {
      const generateFtaBaseUrl = import.meta?.env?.VITE_FTA_BACKEND_URL || DEFAULT_FTA_BASE_URL
      setKbPrimaryFileId(meta.id)
      setKbJob({
        job_id: '',
        status: 'running',
        stage: 'chunk',
        progress: 30,
        message: `正在导入工单数据…（${file.name}）`,
      })
      importWorkOrders({
        file,
        outputDir: './output',
        fileId: plan?.workOrderConfig?.fileId || undefined,
        fieldMapping: plan?.fieldMapping || {},
        syncToGenerateFta: plan?.workOrderConfig?.syncToGenerateFta !== false,
        generateFtaBaseUrl,
        clearGraphBeforeImport: false,
      })
        .then((resp) => {
          updateKbFileAfterDirectImport(meta.id, resp, 'work_order')
          setKbJob({
            job_id: '',
            status: resp?.status === 'completed_with_sync_error' ? 'completed_with_sync_error' : 'success',
            stage: resp?.status === 'completed_with_sync_error' ? 'completed_with_sync_error' : 'success',
            progress: 100,
            message: `工单导入完成（${Number(resp?.imported?.work_orders || 0)} 条）`,
            sync_error: resp?.sync_error || '',
            sync_response: resp?.sync_response,
          })
        })
        .catch((e) => {
          setKbJob({
            job_id: '',
            status: 'failed',
            stage: 'failed',
            progress: 100,
            message: '工单导入失败',
            error: e?.message || '工单导入失败',
          })
          markKbFileImportFailed(meta.id, 'work_order', e)
        })
    },
    [markKbFileImportFailed, updateKbFileAfterDirectImport],
  )

  const startMaintenanceImportForFile = useCallback(
    (meta, file, plan) => {
      const generateFtaBaseUrl = import.meta?.env?.VITE_FTA_BACKEND_URL || DEFAULT_FTA_BASE_URL
      setKbPrimaryFileId(meta.id)
      setKbJob({
        job_id: '',
        status: 'running',
        stage: 'entity',
        progress: 35,
        message: `正在导入维修记录…（${file.name}）`,
      })
      importMaintenanceCases({
        file,
        outputDir: './output',
        caseIdPrefix: plan?.maintenanceConfig?.caseIdPrefix || 'case',
        maxSummaryChars: Number(plan?.maintenanceConfig?.maxSummaryChars) || 800,
        skipEntity: Boolean(plan?.maintenanceConfig?.skipEntity),
        skipRelation: Boolean(plan?.maintenanceConfig?.skipRelation),
        syncToGenerateFta: plan?.maintenanceConfig?.syncToGenerateFta !== false,
        generateFtaBaseUrl,
        clearGraphBeforeImport: false,
      })
        .then((resp) => {
          updateKbFileAfterDirectImport(meta.id, resp, 'maintenance_record')
          const count = Number(resp?.counts?.maintenance_cases || 0)
          setKbJob({
            job_id: '',
            status: resp?.status === 'completed_with_sync_error' ? 'completed_with_sync_error' : 'success',
            stage: resp?.status === 'completed_with_sync_error' ? 'completed_with_sync_error' : 'success',
            progress: 100,
            message: `维修记录导入完成（${count} 个案例）`,
            sync_error: resp?.sync_error || '',
            sync_response: resp?.sync_response,
          })
        })
        .catch((e) => {
          setKbJob({
            job_id: '',
            status: 'failed',
            stage: 'failed',
            progress: 100,
            message: '维修记录导入失败',
            error: e?.message || '维修记录导入失败',
          })
          markKbFileImportFailed(meta.id, 'maintenance_record', e)
        })
    },
    [markKbFileImportFailed, updateKbFileAfterDirectImport],
  )

  const handleImportPlans = useCallback(
    (plans) => {
      const wasEmpty = files.length === 0
      const batchId = Date.now()
      const incoming = (plans || []).map((plan, idx) => {
        const id = `${batchId}-${idx}`
        fileObjectStoreRef.current.set(id, plan.file)
        return {
          id,
          name: plan.file.name,
          size: plan.file.size,
          uploadProgress: 100,
          parseProgress: 0,
          status: 'processing',
          kbCategory: plan.category,
          kbCategoryLabel: kbCategoryLabel(plan.category),
          sourceType: inferSourceTypeFromCategory(plan.category),
        }
      })

      setFiles([...incoming, ...files])
      setKbChunkIncludeById((prev) => {
        const next = { ...prev }
        for (const inc of incoming) next[inc.id] = true
        return next
      })
      bumpKbDatasetEpoch()
      setSelectedFileId(null)
      if (wasEmpty && incoming.length && projectId) {
        const updated = renameProjectFromFirstFile(projectId, incoming[0].name)
        if (updated) setProjectName(updated.name)
      }

      incoming.forEach((meta, idx) => {
        const plan = plans[idx]
        if (!plan?.file) return
        if (plan.category === 'document') {
          startDocumentImportForFile(meta, plan.file, plan)
          return
        }
        if (plan.category === 'work_order') {
          startWorkOrderImportForFile(meta, plan.file, plan)
          return
        }
        if (plan.category === 'maintenance_record') {
          startMaintenanceImportForFile(meta, plan.file, plan)
        }
      })
    },
    [
      files,
      projectId,
      startDocumentImportForFile,
      startMaintenanceImportForFile,
      startWorkOrderImportForFile,
    ],
  )

  useEffect(() => {
    kbPrimaryFileIdRef.current = kbPrimaryFileId || ''
  }, [kbPrimaryFileId])

  useEffect(() => {
    if (!projectId) return
    setWorkspaceReady(false)
    fileObjectStoreRef.current = new Map()
    setSelectedFileId(null)
    setKbJob(null)
    setKbPrimaryFileId('')
    setKbHydrating(false)
    kbJobIdByFileIdRef.current = new Map()
    if (kbPollerRef.current) {
      kbPollerRef.current.abort()
      kbPollerRef.current = null
    }
    // stop per-file pollers
    try {
      for (const c of kbPollersByFileIdRef.current.values()) c.abort?.()
    } catch {
      // ignore
    }
    kbPollersByFileIdRef.current = new Map()
    ensureProject(projectId)
    const proj = getProjectById(projectId)
    if (proj) setProjectName(proj.name)

    const ws = getWorkspace(projectId)
    if (ws) {
      setFiles((ws.files || []).map(migrateKbFileFromWorkspace))
      setKbDatasetEpoch(typeof ws.kbDatasetEpoch === 'number' ? ws.kbDatasetEpoch : 0)
      if (Array.isArray(ws.kbSourceFileIds)) {
        const inc = {}
        for (const f of ws.files || []) {
          inc[f.id] = ws.kbSourceFileIds.includes(f.id)
        }
        setKbChunkIncludeById(inc)
      } else {
        setKbChunkIncludeById({})
      }
    } else {
      setFiles([])
      setKbDatasetEpoch(0)
      setKbChunkIncludeById({})
    }

    setWorkspaceReady(true)
  }, [projectId])

  // 重新进入页面：恢复每个文件的 KB 轮询（基于持久化的 kbJobId）
  const kbResumeOnceRef = useRef(false)
  useEffect(() => {
    if (!workspaceReady) return
    if (kbResumeOnceRef.current) return
    kbResumeOnceRef.current = true
    const list = Array.isArray(files) ? files : []
    // 仅对仍需向 KB 查询状态的文件恢复轮询；已成功持久化的文件不再请求（避免 KB 停机时误判失败）
    const needsKbPoll = (f) =>
      f?.kbJobId &&
      !f.kbImportComplete &&
      (f.status === 'processing' || f.status === 'failed')
    // 选择一个“主流程”文件：优先仍在构建中的，其次曾失败需重试的
    const processing = list.find((f) => f?.kbJobId && f.status === 'processing')
    const primary = processing || list.find(needsKbPoll) || null
    if (primary?.id && primary?.kbJobId && needsKbPoll(primary)) {
      setKbPrimaryFileId(primary.id)
      setKbHydrating(true)
      setKbJob({
        job_id: String(primary.kbJobId || ''),
        status: 'running',
        stage: 'resume',
        progress: Number(primary.parseProgress) || 0,
        message: `正在恢复任务进度…（${primary.name || '文件'}）`,
      })
    } else if (list.some((f) => f?.kbImportComplete && f.status === 'done')) {
      const firstDone = list.find((f) => f.status === 'done')
      if (firstDone?.id) setKbPrimaryFileId(firstDone.id)
      setKbHydrating(false)
    }
    for (const f of list) {
      const jobId = f?.kbJobId
      if (jobId && needsKbPoll(f)) {
        startKbPollingForFile(f.id, jobId)
      }
    }
  }, [workspaceReady, files, startKbPollingForFile])

  useEffect(() => {
    setLinkedKbFileIds([])
    setActiveFaultTreeKey(null)
  }, [projectId])

  useEffect(() => {
    setKbChunks([])
    setKbChunksError('')
    setBatchJob(null)
    setBatchJobError('')
    setBatchJobRunning(false)
    batchImportedItemIdsRef.current = new Set()
    if (batchPollerRef.current) {
      batchPollerRef.current.abort()
      batchPollerRef.current = null
    }
  }, [projectId])

  useEffect(() => {
    setKbChunkIncludeById((prev) => {
      const next = { ...prev }
      for (const f of files) {
        if (!(f.id in next)) next[f.id] = f.isActive !== false
      }
      return next
    })
  }, [files])

  const kbIncludedNamesKey = useMemo(
    () =>
      files
        .filter((f) => kbChunkIncludeById[f.id] !== false)
        .map(
          (f) =>
            `${f.id}:${f.name}:${String(f.fileVersionId || '').trim()}:${String(f.fileId || '').trim()}`,
        )
        .sort()
        .join('|'),
    [files, kbChunkIncludeById],
  )

  useEffect(() => {
    const ac = new AbortController()
    setKbChunksLoading(true)
    setKbChunksError('')
    // 临时：预览时直接拉取数据库全库 chunks（后端按 safe_limit 截断）
    listAllChunks({ limit: 800, signal: ac.signal })
      .then((r) => {
        setKbChunks(Array.isArray(r?.chunks) ? r.chunks : [])
      })
      .catch((e) => {
        if (e?.name === 'AbortError') return
        setKbChunksError(e?.message || '加载分块失败')
        setKbChunks([])
      })
      .finally(() => {
        if (!ac.signal.aborted) setKbChunksLoading(false)
      })
    return () => ac.abort()
  }, [kbIncludedNamesKey])

  // 将 KB job 的实时进度映射回“当前触发的那个文件”的解析进度条（与 kbStatusFromJob 终态一致）
  useEffect(() => {
    if (!kbPrimaryFileId) return
    if (!kbJob) return
    const pct = Number.isFinite(Number(kbJob.progress)) ? Math.max(0, Math.min(100, Number(kbJob.progress))) : 0
    const st = String(kbJob.status || '').toLowerCase()
    const failed = st === 'failed' || st === 'error'
    const done =
      !failed &&
      (st === 'success' ||
        st === 'completed' ||
        st === 'finished' ||
        st === 'done' ||
        st === 'succeeded' ||
        st === 'successful' ||
        st === 'completed_with_sync_error')
    setFiles((prev) =>
      prev.map((f) => {
        if (f.id !== kbPrimaryFileId) return f
        const next = {
          ...f,
          parseProgress: failed || done ? 100 : Math.max(f.parseProgress || 0, Math.round(pct)),
          status: failed ? 'failed' : done ? 'done' : 'processing',
        }
        return done ? { ...next, kbImportComplete: true } : next
      }),
    )
  }, [kbJob, kbPrimaryFileId])

  useEffect(() => {
    if (!selectedFileId) return
    if (!files.some((f) => f.id === selectedFileId)) {
      setSelectedFileId(null)
    }
  }, [files, selectedFileId])

  // 若当前未选中任何文件：当有文件进入 done 状态时，自动选中第一个已完成文件（便于预览）
  useEffect(() => {
    if (selectedFileId) return
    const firstDone = files.find((f) => f.status === 'done')
    if (firstDone) setSelectedFileId(firstDone.id)
  }, [files, selectedFileId])

  const kbSourceFileIdsForSave = useMemo(
    () => files.filter((f) => kbChunkIncludeById[f.id] !== false).map((f) => f.id),
    [files, kbChunkIncludeById],
  )

  const ingestBatchItemToLocalCanvas = useCallback(
    async ({ itemId, treeId, topEventLabel }, signal) => {
      if (!projectId || !treeId || !itemId) return
      let ver = await getTree({ treeId: String(treeId), signal })
      const hasTd = Boolean(ver?.tree_data || ver?.treeData)
      if (!hasTd) {
        const cv = Number(ver?.current_version)
        const v = Number.isFinite(cv) && cv >= 1 ? cv : 1
        ver = await getTreeVersion({ treeId: String(treeId), version: v, signal })
      }
      const rawJsonText = JSON.stringify(ver, null, 2)
      const graphData = normalizeBackendTreeDocumentToGraph(ver)
      const canvasId = `tree-${String(treeId)}`
      const srcIds = Array.isArray(batchKbSourceFileIdsRef.current)
        ? batchKbSourceFileIdsRef.current.map(String).filter(Boolean)
        : []
      upsertCanvasDraft(projectId, canvasId, {
        title: inferCanvasDisplayTitle(rawJsonText, graphData),
        rawJsonText,
        graphData,
        selectedSourceFiles: srcIds,
        backendTreeId: String(treeId),
        assistantMessages: [
          {
            role: 'assistant',
            at: Date.now(),
            content: `批量生成：${topEventLabel || '故障树'}（tree_id=${String(treeId)}）`,
          },
        ],
      })
      setFaultTreeRefreshKey((k) => k + 1)
    },
    [projectId],
  )

  useEffect(() => {
    if (!projectId || !workspaceReady) return
    let cancelled = false
    loadExploded3dBundle(projectId)
      .then((b) => {
        if (!cancelled) setExploded3dConfigured(!!b)
      })
      .catch(() => {
        if (!cancelled) setExploded3dConfigured(false)
      })
    return () => {
      cancelled = true
    }
  }, [projectId, workspaceReady])

  const handleExploded3dSaved = useCallback(
    async (bundle) => {
      if (!projectId) return
      await saveExploded3dBundle(projectId, bundle)
      setExploded3dConfigured(true)
      saveWorkspace(projectId, {
        files,
        messages: DEFAULT_WORKSPACE_MESSAGES,
        resultItems: [],
        kbSourceFileIds: kbSourceFileIdsForSave,
        kbDatasetEpoch,
        exploded3dConfigured: true,
      })
    },
    [projectId, files, kbSourceFileIdsForSave, kbDatasetEpoch],
  )

  const handleExploded3dClear = useCallback(async () => {
    if (!projectId) return
    await deleteExploded3dBundle(projectId)
    setExploded3dConfigured(false)
    saveWorkspace(projectId, {
      files,
      messages: DEFAULT_WORKSPACE_MESSAGES,
      resultItems: [],
      kbSourceFileIds: kbSourceFileIdsForSave,
      kbDatasetEpoch,
      exploded3dConfigured: false,
    })
  }, [projectId, files, kbSourceFileIdsForSave, kbDatasetEpoch])

  useEffect(() => {
    if (!projectId || !workspaceReady) return
    saveWorkspace(projectId, {
      files,
      messages: DEFAULT_WORKSPACE_MESSAGES,
      resultItems: [],
      kbSourceFileIds: kbSourceFileIdsForSave,
      kbDatasetEpoch,
      exploded3dConfigured,
    })
  }, [projectId, workspaceReady, files, kbSourceFileIdsForSave, kbDatasetEpoch, exploded3dConfigured])

  useEffect(() => {
    if (!previewModalOpen) return undefined
    const onKey = (e) => {
      if (e.key === 'Escape') {
        setPreviewModalOpen(false)
        setPreviewFileObject(null)
      }
    }
    document.addEventListener('keydown', onKey)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
    }
  }, [previewModalOpen])

  const finishedCount = useMemo(
    () => files.filter((f) => f.status === 'done').length,
    [files],
  )

  useEffect(() => {
    return () => {
      if (kbPollerRef.current) {
        kbPollerRef.current.abort()
        kbPollerRef.current = null
      }
      if (batchPollerRef.current) {
        batchPollerRef.current.abort()
        batchPollerRef.current = null
      }
      try {
        for (const c of kbPollersByFileIdRef.current.values()) c.abort?.()
      } catch {
        // ignore
      }
      kbPollersByFileIdRef.current = new Map()
    }
  }, [])

  const bumpKbDatasetEpoch = useCallback(() => {
    setKbDatasetEpoch((e) => (Number.isFinite(e) ? e + 1 : 1))
  }, [])

  const fileDisplayItems = useMemo(() => {
    const normalizedName = (name) => String(name || '').trim().toLocaleLowerCase()
    const sorted = [...files].sort((a, b) => {
      const groupA = a.fileId || `pending:${normalizedName(a.name)}`
      const groupB = b.fileId || `pending:${normalizedName(b.name)}`
      if (groupA !== groupB) return groupA.localeCompare(groupB)
      return Number(b.versionNo || 0) - Number(a.versionNo || 0)
    })
    let previousGroup = ''
    return sorted.map((file) => {
      const groupKey = file.fileId || `pending:${normalizedName(file.name)}`
      const startsGroup = groupKey !== previousGroup
      previousGroup = groupKey
      return { file, startsGroup, groupKey }
    })
  }, [files])

  const batchScopeFileVersionIds = useMemo(() => {
    // 仅使用“已完成”且具备 fileVersionId 的文件作为选源范围
    const ids = files
      .filter((f) => f.status === 'done' && kbChunkIncludeById[f.id] !== false)
      .map((f) => String(f.fileVersionId || '').trim())
      .filter(Boolean)
    // 去重保持顺序
    const seen = new Set()
    const uniq = []
    for (const id of ids) {
      if (seen.has(id)) continue
      seen.add(id)
      uniq.push(id)
    }
    return uniq
  }, [files, kbChunkIncludeById])

  const startBatchGenerate = useCallback(() => {
    if (!batchScopeFileVersionIds.length) {
      setBatchJobError('暂无可用选源：请先等待至少一个文件完成知识库导入（done）')
      return
    }
    if (batchPollerRef.current) {
      batchPollerRef.current.abort()
      batchPollerRef.current = null
    }
    batchImportedItemIdsRef.current = new Set()
    batchKbSourceFileIdsRef.current = [...kbSourceFileIdsForSave]
    const ac = new AbortController()
    batchPollerRef.current = ac
    setBatchJobError('')
    setBatchJobRunning(true)
    setBatchJob({
      status: 'pending',
      message: '正在提交批量生成任务…',
      selected_file_version_ids: batchScopeFileVersionIds,
    })

    const processItemsForCanvases = (jobPayload) => {
      const items = Array.isArray(jobPayload?.items) ? jobPayload.items : []
      if (!items.length || !projectId) return
      for (const it of items) {
        const itemId = String(it?.item_id || it?.itemId || it?._id || '').trim()
        const treeId = it?.tree_id || it?.treeId
        const st = String(it?.status || '').toLowerCase()
        if (!itemId || st !== 'success' || !treeId) continue
        if (batchImportedItemIdsRef.current.has(itemId)) continue
        batchImportedItemIdsRef.current.add(itemId)
        const topEventLabel = String(it?.top_event || it?.topEvent || '').trim()
        void ingestBatchItemToLocalCanvas(
          { itemId, treeId: String(treeId), topEventLabel },
          ac.signal,
        ).catch((e) => {
          if (e?.name === 'AbortError') return
          batchImportedItemIdsRef.current.delete(itemId)
        })
      }
    }

    generateAllTrees({ signal: ac.signal, selectedFileVersionIds: batchScopeFileVersionIds })
      .then((resp) => {
        setBatchJob(resp || null)
        const jobId = resp?.job_id || resp?.jobId
        if (!jobId) return resp
        return pollBatchJob({
          jobId,
          signal: ac.signal,
          onUpdate: (job) => {
            setBatchJob(job)
            processItemsForCanvases(job)
          },
          intervalMs: 2000,
        })
      })
      .then((finalJob) => {
        setBatchJob(finalJob || null)
        processItemsForCanvases(finalJob)
      })
      .catch((e) => {
        if (e?.name === 'AbortError') return
        setBatchJobError(e?.message || '批量生成失败')
      })
      .finally(() => {
        if (!ac.signal.aborted) setBatchJobRunning(false)
      })
  }, [batchScopeFileVersionIds, projectId, kbSourceFileIdsForSave, ingestBatchItemToLocalCanvas])

  const handleFileChange = (event) => {
    const selected = Array.from(event.target.files || [])
    if (!selected.length) return
    const batchId = Date.now()
    const incoming = selected.map((file, idx) => ({
      id: `${batchId}-${idx}`,
      file,
      name: file.name,
      size: file.size,
    }))
    setPendingImportItems(incoming)
    setImportModalOpen(true)
    event.target.value = ''
  }

  const selectedFileMeta = useMemo(
    () => files.find((f) => f.id === selectedFileId) || null,
    [files, selectedFileId],
  )

  /** 下方列表：按当前选中文件过滤；未选中时展示全部已加载分块 */
  const visibleKbChunks = useMemo(() => {
    if (!selectedFileId || !selectedFileMeta) return kbChunks
    return kbChunks.filter((c) => chunkBelongsToKbFile(c, selectedFileMeta))
  }, [kbChunks, selectedFileId, selectedFileMeta])

  const selectedFileObject = previewFileObject

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

  const openCanvasDraft = (canvasId) => {
    navigate(
      `/fta-viewer?projectId=${encodeURIComponent(projectId)}&canvasId=${encodeURIComponent(canvasId)}`,
    )
  }

  const faultTreeCatalog = useMemo(() => {
    if (!projectId) return []
    const drafts = listCanvasDrafts(projectId)
    return drafts
      .map((d) => {
        const topLabel =
          d.title || inferCanvasDisplayTitle(d.rawJsonText || '', d.graphData || null)
        const msgTs = lastChatTimestampFromMessages(d.assistantMessages)
        const lastAt = msgTs || d.updatedAt || null
        const ids = Array.isArray(d.selectedSourceFiles)
          ? d.selectedSourceFiles.map(String).filter(Boolean)
          : []
        return {
          key: `canvas:${d.canvasId}`,
          canvasId: d.canvasId,
          topLabel,
          lastAt,
          linkedFileIds: ids,
          graphData: d.graphData || null,
        }
      })
      .sort((a, b) => Number(b.lastAt || 0) - Number(a.lastAt || 0))
  }, [projectId, location.key, faultTreeRefreshKey])

  const deleteFaultTreeDraft = useCallback(
    (canvasId) => {
      if (!projectId || !canvasId) return
      const ok = window.confirm('确定删除该故障树草稿？此操作仅影响本地草稿，无法恢复。')
      if (!ok) return
      removeCanvasDraft(projectId, canvasId)
      setFaultTreeRefreshKey((k) => k + 1)
      setActiveFaultTreeKey((prev) => (prev === `canvas:${canvasId}` ? null : prev))
      setLinkedKbFileIds((prev) => (Array.isArray(prev) ? [] : prev))
    },
    [projectId],
  )

  const deleteUploadedFile = useCallback(
    (fileId) => {
      if (!projectId || !fileId) return
      const f = files.find((x) => x.id === fileId)
      const name = f?.name || fileId
      const ok = window.confirm(`确定删除文件「${name}」？此操作仅删除本地记录与预览文件对象，不会删除后端已导入的数据。`)
      if (!ok) return

      try {
        const controller = kbPollersByFileIdRef.current.get(fileId)
        controller?.abort?.()
        kbPollersByFileIdRef.current.delete(fileId)
      } catch {
        // ignore
      }

      try {
        fileObjectStoreRef.current.delete(fileId)
      } catch {
        // ignore
      }

      setFiles((prev) => prev.filter((x) => x.id !== fileId))
      setKbChunkIncludeById((prev) => {
        const next = { ...(prev || {}) }
        delete next[fileId]
        return next
      })
      bumpKbDatasetEpoch()

      setSelectedFileId((prev) => (prev === fileId ? null : prev))
      setPreviewModalOpen(false)
      setLinkedKbFileIds((prev) => (Array.isArray(prev) ? prev.filter((id) => id !== fileId) : prev))
    },
    [projectId, files, bumpKbDatasetEpoch],
  )

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
            <p className="home-subtitle">知识库构建 · 故障树画布 · AI 辅助编辑</p>
          </div>
          <ThemeToggle />
        </header>
      </div>

      <main className="home-main">
        <section className="home-panel home-panel--left">
          <h2 className="home-panel-title">知识库构建</h2>
          <p className="home-panel-desc">
            上传设备手册、维修记录等资料，完成解析与知识抽取后可作为故障树生成的依据。分块预览来自 FTA
            KB 后端（localhost:8010）数据库中与勾选文件匹配的 chunks。
          </p>

          <div className="home-panel-scroll">
            <div className="home-upload-row">
              <label className="home-upload-btn">
                上传文件
                <input type="file" multiple accept=".pdf,.md,.txt,.csv,.xlsx,.docx" onChange={handleFileChange} />
              </label>
              <button type="button" className="home-3d-btn" onClick={openThreeDModal} title="上传 GLB 与部件列表">
                三维
              </button>
              {exploded3dConfigured ? (
                <span className="home-3d-hint">已配置三维爆炸图与部件列表</span>
              ) : (
                <span className="home-3d-hint">未配置三维（可选）</span>
              )}
            </div>

            <p className="home-upload-summary">
              共 {files.length} 个文件，已完成本地处理 {finishedCount} 个
            </p>

            <KnowledgeBasePanel job={kbJob} onOpenGraph={openGraphModal} hydrating={kbHydrating} />

            <h3 className="home-files-section-title">已上传文件</h3>

            <div className="home-file-list home-file-list--kb">
              {files.length === 0 && (
                <div className="home-empty">暂无文件。请先上传设备资料。</div>
              )}
              {fileDisplayItems.map(({ file, startsGroup, groupKey }) => (
                <Fragment key={file.id}>
                  {startsGroup ? (
                    <div className="home-file-version-group" title={groupKey}>
                      <span>{file.name}</span>
                      <span>{file.fileId ? '\u7248\u672c\u8bb0\u5f55' : '\u5f85\u5206\u914d\u7248\u672c'}</span>
                    </div>
                  ) : null}
                <div
                  className={`home-file-row${
                    file.id === selectedFileId ? ' home-file-row--selected' : ''
                  }${linkedKbFileIds.includes(file.id) ? ' home-file-row--linked' : ''}${
                    file.status !== 'done' ? ' home-file-row--processing' : ''
                  }`}
                >
                  <button
                    type="button"
                    className="home-file-row-main"
                    onClick={() => {
                      setSelectedFileId(file.id)
                      setPreviewFileObject(fileObjectStoreRef.current.get(file.id) || null)
                    }}
                    title="点击选中该文件，在下方「分块预览」中查看与该文件匹配的分块；知识库处理中时分块可能尚未就绪"
                  >
                    <div className="home-file-card-head">
                      <div className="home-file-card-name" title={file.name}>
                        {file.name}
                      </div>
                      <div className="home-file-card-meta">
                        {file.kbCategoryLabel ? (
                          <span className="home-file-pill home-file-pill--type">{file.kbCategoryLabel}</span>
                        ) : null}
                        <span className={`home-file-pill home-file-pill--${file.status || 'processing'}`}>
                          {file.status === 'done' ? '已完成' : file.status === 'failed' ? '失败' : '处理中'}
                        </span>
                        <span className="home-file-size">{(file.size / 1024).toFixed(1)} KB</span>
                      </div>
                    </div>

                    <div className="home-file-bars">
                      <div className="home-file-bar">
                        <span className="home-file-bar-label">上传</span>
                        <div className="home-file-bar-track" aria-hidden>
                          <div
                            className="home-file-bar-fill"
                            style={{ width: `${Math.max(0, Math.min(100, Number(file.uploadProgress) || 0))}%` }}
                          />
                        </div>
                        <span className="home-file-bar-num">{Math.max(0, Math.min(100, Number(file.uploadProgress) || 0))}%</span>
                      </div>
                      <div className="home-file-bar">
                        <span className="home-file-bar-label">解析</span>
                        <div className="home-file-bar-track home-file-bar-track--parse" aria-hidden>
                          <div
                            className="home-file-bar-fill home-file-bar-fill--parse"
                            style={{ width: `${Math.max(0, Math.min(100, Number(file.parseProgress) || 0))}%` }}
                          />
                        </div>
                        <span className="home-file-bar-num">{Math.max(0, Math.min(100, Number(file.parseProgress) || 0))}%</span>
                      </div>
                    </div>
                    {file.fileVersionId ? (
                      <div className="home-file-import-meta">
                        <span>版本：{file.fileVersionId}</span>
                        {file.resultSummary ? (
                          <span>
                            摘要：
                            {Object.entries(file.resultSummary)
                              .filter(([, value]) => Number.isFinite(Number(value)))
                              .map(([key, value]) => `${key}=${value}`)
                              .join(' · ')}
                          </span>
                        ) : null}
                      </div>
                    ) : null}
                  </button>
                  {file.status === 'done' ? (
                    <button
                      type="button"
                      className="home-file-doc-btn"
                      title="预览原文（PDF / TXT）"
                      aria-label="预览原文"
                      onClick={(e) => {
                        e.stopPropagation()
                        setSelectedFileId(file.id)
                        setPreviewFileObject(fileObjectStoreRef.current.get(file.id) || null)
                        setPreviewModalOpen(true)
                      }}
                    >
                      原文
                    </button>
                  ) : null}
                  <label className="home-file-row-check" title="在下方分块预览中包含此文件对应的知识分块">
                    <input
                      type="checkbox"
                      checked={kbChunkIncludeById[file.id] !== false}
                      onChange={(e) => {
                        setKbChunkIncludeById((prev) => ({
                          ...prev,
                          [file.id]: e.target.checked,
                        }))
                        bumpKbDatasetEpoch()
                      }}
                    />
                  </label>
                  <button
                    type="button"
                    className="home-icon-btn home-icon-btn--danger"
                    title="删除文件（仅本地）"
                    aria-label="删除文件"
                    onClick={(e) => {
                      e.stopPropagation()
                      deleteUploadedFile(file.id)
                    }}
                  >
                    ×
                  </button>
                </div>
                </Fragment>
              ))}
            </div>

            <div className="home-kbchunks-panel">
              <button
                type="button"
                className="home-collapse-head"
                onClick={() => setKbChunksPanelOpen((o) => !o)}
              >
                <span className="home-collapse-head-title">分块（CHUNKS）预览</span>
                <span className="home-collapse-head-ico" aria-hidden>
                  {kbChunksPanelOpen ? '▼' : '▶'}
                </span>
              </button>
              {kbChunksPanelOpen ? (
                <div className="home-kbchunks-body">
                  {selectedFileMeta ? (
                    <div className="home-kbchunks-focus">
                      当前预览文件：<strong title={selectedFileMeta.name}>{selectedFileMeta.name}</strong>
                      <span className="home-kbchunks-focus-count">
                        （{visibleKbChunks.length}/{kbChunks.length} 条分块）
                      </span>
                    </div>
                  ) : null}
                  {kbChunksLoading ? (
                    <div className="home-empty home-kbchunks-status">正在从后端加载…</div>
                  ) : null}
                  {kbChunksError ? <div className="home-kbchunks-err">{kbChunksError}</div> : null}
                  {!kbChunksLoading && !kbChunksError && kbChunks.length === 0 ? (
                    <div className="home-empty home-kbchunks-status">
                      勾选上方文件后，将展示数据库中与之文本匹配的 chunks（需后端已导入分块数据）。点击某个文件即可选中并在下方过滤显示其分块。
                    </div>
                  ) : null}
                  {!kbChunksLoading &&
                  !kbChunksError &&
                  kbChunks.length > 0 &&
                  visibleKbChunks.length === 0 &&
                  selectedFileMeta ? (
                    <div className="home-empty home-kbchunks-status">
                      暂无与「{selectedFileMeta.name}」匹配的分块（可能仍在同步，或后端 file_version_id
                      与本地不一致）。可稍后再试或检查 FTA-GNR 中该文件的 chunks。
                    </div>
                  ) : null}
                  {visibleKbChunks.length > 0 ? (
                    <ul className="home-kb-chunk-list">
                      {visibleKbChunks.map((c, idx) => {
                        const cid = c.id ?? c.chunk_id ?? `idx-${idx}`
                        const includedFiles = files.filter((f) => kbChunkIncludeById[f.id] !== false)
                        const fromFile = matchChunkToKbFile(c, includedFiles)
                        return (
                          <li key={`${String(cid)}-${idx}`} className="home-kb-chunk-card">
                            <div className="home-kb-chunk-meta">
                              <span className="home-kb-chunk-id">#{String(cid)}</span>
                              <span className="home-kb-chunk-file" title={fromFile}>
                                {fromFile}
                              </span>
                            </div>
                            <p className="home-kb-chunk-text">{getChunkBodyText(c)}</p>
                          </li>
                        )
                      })}
                    </ul>
                  ) : null}
                </div>
              ) : null}
            </div>
          </div>
        </section>

        <section className="home-panel home-panel--fault-trees">
          <h2 className="home-panel-title">故障树</h2>
          <p className="home-panel-desc">
            本项目故障树均在「新建空白画布」中创建与编辑。此处列出各画布草稿；点击卡片可在左侧高亮该画布内已选知识库文件；缩略图为结构示意。
          </p>

          <div className="home-ft-toolbar">
            <button
              type="button"
              className="home-send-btn"
              onClick={() =>
                navigate(`/fta-viewer?projectId=${encodeURIComponent(projectId)}&blank=1`)
              }
              title="新建空白画布"
            >
              新建空白画布
            </button>
            <button
              type="button"
              className="home-send-btn"
              onClick={startBatchGenerate}
              disabled={batchJobRunning || batchScopeFileVersionIds.length === 0}
              title={
                batchScopeFileVersionIds.length === 0
                  ? '请先等待至少一个文件完成知识库导入（done）'
                  : batchJobRunning
                    ? '批量生成进行中…'
                    : '批量生成当前选源范围内的全部顶事件故障树'
              }
            >
              {batchJobRunning ? '批量生成中…' : '批量生成'}
            </button>
            {batchJobRunning || batchJob ? (
              <span className="home-ft-batch-inline" role="status" aria-live="polite">
                {formatBatchProgressInline(batchJob, batchJobRunning)}
              </span>
            ) : null}
          </div>

          {batchJobError ? (
            <div className="home-ft-batch-status home-ft-batch-status--compact" role="alert">
              <div className="home-ft-batch-err">错误：{batchJobError}</div>
            </div>
          ) : null}
          {batchJob && (batchJob.job_id || batchJob.jobId) && !batchJobRunning ? (
            <div className="home-ft-batch-meta" role="status">
              任务 ID：{String(batchJob.job_id || batchJob.jobId)}
            </div>
          ) : null}

          <div className="home-ft-grid">
            {faultTreeCatalog.length === 0 && (
              <div className="home-empty home-ft-grid-empty">
                暂无故障树画布。请点击「新建空白画布」，在画布页通过 AI 助手生成或编辑故障树。
              </div>
            )}
            {faultTreeCatalog.map((entry) => (
              <div
                key={entry.key}
                role="button"
                tabIndex={0}
                className={`home-ft-card${
                  activeFaultTreeKey === entry.key ? ' home-ft-card--active' : ''
                }`}
                onClick={() => {
                  setActiveFaultTreeKey(entry.key)
                  setLinkedKbFileIds(entry.linkedFileIds || [])
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    setActiveFaultTreeKey(entry.key)
                    setLinkedKbFileIds(entry.linkedFileIds || [])
                  }
                }}
              >
                <button
                  type="button"
                  className="home-ft-del-btn"
                  title="删除草稿（仅本地）"
                  aria-label="删除草稿"
                  onClick={(e) => {
                    e.stopPropagation()
                    if (entry.canvasId) deleteFaultTreeDraft(entry.canvasId)
                  }}
                >
                  ×
                </button>
                <div className="home-ft-thumb" aria-hidden>
                  <MiniFaultTreeThumbnail graphData={entry.graphData} />
                </div>
                <div className="home-ft-card-body">
                  <div className="home-ft-card-title" title={entry.topLabel}>
                    {entry.topLabel}
                  </div>
                  <div className="home-ft-card-meta">
                    <span className="home-ft-card-badge">画布草稿</span>
                    <span className="home-ft-card-time">
                      最近对话：{formatFaultTreeChatTime(entry.lastAt)}
                    </span>
                  </div>
                </div>
                <button
                  type="button"
                  className="home-ft-enter-btn"
                  onClick={(e) => {
                    e.stopPropagation()
                    if (entry.canvasId) openCanvasDraft(entry.canvasId)
                  }}
                >
                  进入画布
                </button>
              </div>
            ))}
          </div>
        </section>
      </main>

      <KnowledgeGraphModal open={graphModalOpen} onClose={closeGraphModal} />

      <Exploded3dUploadModal
        open={threeDModalOpen}
        onClose={closeThreeDModal}
        projectId={projectId}
        onSaved={handleExploded3dSaved}
        onClear={handleExploded3dClear}
      />

      <KnowledgeImportModal
        open={importModalOpen}
        items={pendingImportItems}
        busy={importSubmitting}
        onClose={() => {
          if (importSubmitting) return
          setImportModalOpen(false)
          setPendingImportItems([])
        }}
        onProfileWorkOrder={profileWorkOrderForModal}
        onConfirm={(plans) => {
          const resolvedPlans = plans.map((plan) => ({
            ...plan,
            file: pendingImportItems.find((item) => item.id === plan.id)?.file || null,
          }))
          setImportSubmitting(true)
          try {
            handleImportPlans(resolvedPlans.filter((plan) => plan.file))
            setImportModalOpen(false)
            setPendingImportItems([])
          } finally {
            setImportSubmitting(false)
          }
        }}
      />

      {previewModalOpen && (
        <div
          className="home-preview-modal-overlay"
          role="presentation"
          onClick={() => {
            setPreviewModalOpen(false)
            setPreviewFileObject(null)
          }}
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
                onClick={() => {
                  setPreviewModalOpen(false)
                  setPreviewFileObject(null)
                }}
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
