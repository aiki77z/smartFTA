const DEFAULT_BASE_URL = 'http://localhost:8010'

function getBaseUrl() {
  const envUrl = import.meta?.env?.VITE_KB_BACKEND_URL
  return (envUrl || DEFAULT_BASE_URL).replace(/\/+$/, '')
}

async function requestJson(path, { method = 'GET', body, signal, headers } = {}) {
  const baseUrl = getBaseUrl()
  const url = `${baseUrl}${path.startsWith('/') ? path : `/${path}`}`

  let res
  try {
    res = await fetch(url, { method, body, signal, headers })
  } catch (e) {
    const msg = String(e?.message || '').toLowerCase()
    if (e?.name === 'AbortError' || msg.includes('aborted') || msg.includes('signal is aborted')) {
      const err = new Error('Request aborted')
      err.name = 'AbortError'
      throw err
    }
    throw e
  }

  if (res.ok) return await res.json()

  let detail = ''
  try {
    const data = await res.json()
    detail = data?.detail ? JSON.stringify(data.detail) : JSON.stringify(data)
  } catch {
    try {
      detail = await res.text()
    } catch {
      detail = ''
    }
  }
  const err = new Error(`KB 后端接口错误（${res.status}）：${detail || res.statusText}`)
  err.status = res.status
  err.detail = detail
  throw err
}

function delayWithAbort(ms, signal) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms)
    if (!signal) return
    if (signal.aborted) {
      clearTimeout(t)
      const err = new Error('Request aborted')
      err.name = 'AbortError'
      reject(err)
      return
    }
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(t)
        const err = new Error('Request aborted')
        err.name = 'AbortError'
        reject(err)
      },
      { once: true },
    )
  })
}

export function startKbJobUpload({
  file,
  outputDir = './output',
  chunkSize = 800,
  skipMineru = false,
  skipEntity = false,
  skipRelation = false,
  printRawText = false,
  syncToGenerateFta = true,
  generateFtaBaseUrl,
  // GNR 版本化知识库模式下禁止 clear_graph=true（会直接 400）
  clearGraphBeforeImport = false,
  signal,
} = {}) {
  if (!file) throw new Error('缺少上传文件')
  const form = new FormData()
  form.append('file', file)
  form.append('output_dir', outputDir)
  form.append('chunk_size', String(chunkSize))
  form.append('skip_mineru', String(Boolean(skipMineru)))
  form.append('skip_entity', String(Boolean(skipEntity)))
  form.append('skip_relation', String(Boolean(skipRelation)))
  form.append('print_raw_text', String(Boolean(printRawText)))
  form.append('sync_to_generate_fta', String(Boolean(syncToGenerateFta)))
  if (generateFtaBaseUrl) form.append('generate_fta_base_url', generateFtaBaseUrl)
  form.append('clear_graph_before_import', String(Boolean(clearGraphBeforeImport)))

  return requestJson('/api/kb/jobs/run-upload', { method: 'POST', body: form, signal })
}

export function getKbJob({ jobId, signal } = {}) {
  if (!jobId) throw new Error('缺少 jobId')
  return requestJson(`/api/kb/jobs/${encodeURIComponent(jobId)}`, { signal })
}

export async function downloadKbJobUploadedFile({ jobId, signal } = {}) {
  if (!jobId) throw new Error('缺少 jobId')
  const baseUrl = getBaseUrl()
  const url = `${baseUrl}/api/kb/jobs/${encodeURIComponent(jobId)}/download`
  const res = await fetch(url, { signal })
  if (!res.ok) {
    let detail = ''
    try {
      detail = await res.text()
    } catch {
      detail = ''
    }
    throw new Error(`KB 下载失败（${res.status}）：${detail || res.statusText}`)
  }
  return await res.blob()
}

export async function pollKbJob({ jobId, signal, onUpdate, intervalMs = 900 } = {}) {
  for (;;) {
    const job = await getKbJob({ jobId, signal })
    onUpdate?.(job)
    const st = String(job?.status || '').toLowerCase()
    if (
      st === 'success' ||
      st === 'failed' ||
      st === 'completed_with_sync_error' ||
      st === 'completed' ||
      st === 'finished'
    ) {
      return job
    }
    await delayWithAbort(intervalMs, signal)
  }
}

