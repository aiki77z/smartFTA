/**
 * 知识库构建阶段（对齐 FTA-KB + 下游同步到 FTA-GNR 的真实流程）：
 * - Parse: 解析文件（PDF->MD + 清理 MD 标题层级）
 * - Chunk: 文本分块
 * - Entity: 实体提取（可跳过）
 * - Relation: 关系提取（可跳过）
 * - Sync: 调用 FTA-GNR /api/integration/import-knowledge-artifacts 导入到 Mongo/Neo4j
 */
export const KB_BUILD_STAGES = [
  { id: 'parse', label: '解析文件' },
  { id: 'chunk', label: '文本分块' },
  { id: 'entity', label: '实体抽取' },
  { id: 'relation', label: '关系抽取' },
  { id: 'sync', label: '导入图谱' },
]

/**
 * 与 KnowledgeBasePanel 一致的 job / 演示阶段解析，供横向时间轴使用。
 * @returns {{ stageIndex: number, progress: number, message: string, error: string, st: string, stage: string, fillPercent: number, allDone: boolean }}
 */
export function computeKbTimelineState(job, phase) {
  const normalized = (() => {
    if (job && typeof job === 'object') {
      const st = String(job.status || '').toLowerCase()
      const stage = String(job.stage || '').toLowerCase()
      const progress = Number.isFinite(Number(job.progress)) ? Number(job.progress) : 0
      const message = String(job.message || '').trim()
      const error = job.sync_error || job.error || ''
      return { st, stage, progress, message, error }
    }
    return { st: '', stage: '', progress: 0, message: '', error: '' }
  })()

  const stageIndex = (() => {
    if (job) {
      const s = normalized.stage
      const st = normalized.st
      if (st === 'failed' || s === 'failed') return 0
      if (st === 'success' || s === 'success') return KB_BUILD_STAGES.length
      if (s === 'syncing' || st === 'syncing') return 4
      if (s === 'relation') return 3
      if (s === 'entity') return 2
      if (s === 'chunk') return 1
      // 兼容历史后端 stage：mineru/clean 统一映射到 parse
      if (s === 'parse' || s === 'mineru' || s === 'clean' || s === 'prepare' || st === 'running' || st === 'queued') {
        return 0
      }
      // 兼容后端返回 completed_with_sync_error：流水线完成但同步失败，也视为已到最后阶段
      if (st === 'completed_with_sync_error' || s === 'completed_with_sync_error') return KB_BUILD_STAGES.length
      return 0
    }
    switch (phase) {
      case 'idle':
        return -1
      case 'entity':
        return 3
      case 'relation':
        return 4
      case 'graph':
        return 5
      case 'complete':
        return KB_BUILD_STAGES.length
      default:
        return -1
    }
  })()

  const p = Math.max(0, Math.min(100, normalized.progress))
  const allDone =
    Boolean(job) &&
    (normalized.st === 'success' ||
      normalized.stage === 'success' ||
      normalized.st === 'completed_with_sync_error' ||
      normalized.stage === 'completed_with_sync_error' ||
      stageIndex >= KB_BUILD_STAGES.length)

  let fillPercent = 0
  if (allDone) {
    fillPercent = 100
  } else if (stageIndex < 0) {
    fillPercent = 0
  } else if (stageIndex >= KB_BUILD_STAGES.length) {
    fillPercent = 100
  } else {
    fillPercent = Math.min(100, ((stageIndex + p / 100) / KB_BUILD_STAGES.length) * 100)
  }

  return {
    ...normalized,
    stageIndex,
    fillPercent,
    allDone,
  }
}

const FILE_VERSION_SUFFIX_RE = /_v\d+$/i

/**
 * chunks 表按 file_version_id 存储（常见形如 `{file_id}_v1`），而 KB 同步字段偶发只回填 `file_id`。
 * 展开候选 ID，避免仅用 file_id 查询时无法命中分块。
 * @param {Array<{ fileVersionId?: string, fileId?: string }>} files
 * @returns {string[]}
 */
export function expandFileVersionIdsForChunkQuery(files) {
  const seen = new Set()
  const out = []
  for (const f of files || []) {
    if (!f || typeof f !== 'object') continue
    const fv = String(f.fileVersionId || '').trim()
    const fid = String(f.fileId || '').trim()
    /** @type {Set<string>} */
    const cands = new Set()
    if (fv) cands.add(fv)
    if (fid) cands.add(fid)
    if (fid && !FILE_VERSION_SUFFIX_RE.test(fid)) cands.add(`${fid}_v1`)
    if (fv && !FILE_VERSION_SUFFIX_RE.test(fv)) cands.add(`${fv}_v1`)
    if (fv && fid && fv === fid) cands.add(`${fid}_v1`)
    for (const id of cands) {
      const s = String(id).trim()
      if (!s || seen.has(s)) continue
      seen.add(s)
      out.push(s)
    }
  }
  return out
}

/** 合并两次 chunks 查询结果，按 chunk_uid / id 去重 */
export function mergeKbChunksByIdentity(a, b) {
  const map = new Map()
  for (const c of [...(a || []), ...(b || [])]) {
    if (!c || typeof c !== 'object') continue
    const k =
      (c.chunk_uid != null && String(c.chunk_uid)) ||
      (c.id != null && String(c.id)) ||
      (c.chunk_id != null && String(c.chunk_id))
    if (!k || map.has(k)) continue
    map.set(k, c)
  }
  return [...map.values()]
}

/** 从 chunk 文档取展示文本 */
export function getChunkBodyText(chunk) {
  if (!chunk || typeof chunk !== 'object') return ''
  const t =
    chunk.text ??
    chunk.content ??
    chunk.raw_text ??
    chunk.chunk_text ??
    chunk.body ??
    ''
  return typeof t === 'string' ? t : String(t || '')
}

/** 标明 chunk 更可能属于哪个勾选文件名（子串匹配） */
export function matchChunkToFileName(chunk, fileNames) {
  const parts = []
  for (const key of ['source', 'chunk_name', 'file', 'doc_name', 'path']) {
    const v = chunk?.[key]
    if (typeof v === 'string') parts.push(v.toLowerCase())
  }
  const blob = parts.join(' ')
  for (const name of fileNames || []) {
    const base = String(name)
      .trim()
      .replace(/\\/g, '/')
      .split('/')
      .pop()
      .toLowerCase()
    if (base && blob.includes(base)) return name
  }
  return fileNames?.[0] || '—'
}

/**
 * 优先用 file_version_id / file_id 与本地文件记录对齐，其次再用文件名子串匹配。
 * @param {object} chunk
 * @param {Array<{ id: string, name?: string, fileVersionId?: string, fileId?: string }>} files
 */
export function matchChunkToKbFile(chunk, files) {
  const fv = chunk?.file_version_id != null ? String(chunk.file_version_id) : ''
  const fid = chunk?.file_id != null ? String(chunk.file_id) : ''
  for (const f of files || []) {
    if (!f?.id) continue
    const lfv = String(f.fileVersionId || '').trim()
    const lfid = String(f.fileId || '').trim()
    if (fv && lfv && fv === lfv) return f.name || lfv
    if (fid && lfid && fid === lfid) return f.name || lfid
    if (fv && lfid && (fv === lfid || fv === `${lfid}_v1`)) return f.name || fv
    if (fv && lfv && (fv === lfv || fv === `${lfv}_v1` || `${lfv}_v1` === fv)) return f.name || fv
  }
  const names = (files || []).map((f) => f.name).filter(Boolean)
  return matchChunkToFileName(chunk, names)
}

/**
 * 判断 chunk 是否属于指定本地文件（用于按选中文件过滤列表；无匹配则 false，不默认归到首个文件）。
 * @param {object} chunk
 * @param {{ name?: string, fileVersionId?: string, fileId?: string }} file
 */
export function chunkBelongsToKbFile(chunk, file) {
  if (!chunk || typeof chunk !== 'object' || !file) return false
  const fv = chunk.file_version_id != null ? String(chunk.file_version_id) : ''
  const fid = chunk.file_id != null ? String(chunk.file_id) : ''
  const lfv = String(file.fileVersionId || '').trim()
  const lfid = String(file.fileId || '').trim()
  if (fv && lfv && fv === lfv) return true
  if (fid && lfid && fid === lfid) return true
  if (fv && lfid && (fv === lfid || fv === `${lfid}_v1`)) return true
  if (fv && lfv && (fv === lfv || fv === `${lfv}_v1` || `${lfv}_v1` === fv)) return true
  if (!file.name) return false
  const parts = []
  for (const key of ['source', 'chunk_name', 'file', 'doc_name', 'path']) {
    const v = chunk[key]
    if (typeof v === 'string') parts.push(v.toLowerCase())
  }
  const blob = parts.join(' ')
  const base = String(file.name)
    .trim()
    .replace(/\\/g, '/')
    .split('/')
    .pop()
    .toLowerCase()
  return Boolean(base && blob.includes(base))
}
