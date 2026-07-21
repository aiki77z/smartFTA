import { useEffect, useMemo, useRef, useState } from 'react'

const CATEGORY_OPTIONS = [
  { value: 'document', label: '文档资料', desc: 'PDF / Markdown / TXT 走通用知识抽取链路' },
  { value: 'work_order', label: '工单数据', desc: 'CSV / XLSX 做字段画像；DOCX 按 PR 表单自动解析' },
  { value: 'maintenance_record', label: '维修记录', desc: 'CSV / DOCX / PDF / MD，按维修案例切分并生成 case_summary' },
]

const WORK_ORDER_FIELDS = [
  { key: 'work_order_no', label: '工单号', required: true },
  { key: 'device_id', label: '设备编号', required: false },
  { key: 'device_name', label: '设备名称', required: false },
  { key: 'fault_time', label: '故障时间', required: false },
  { key: 'fault_phenomenon', label: '故障现象', required: true },
  { key: 'alarm_code', label: '报警码', required: false },
  { key: 'fault_cause', label: '故障原因', required: false },
  { key: 'handling_action', label: '处理措施', required: false },
  { key: 'replaced_parts', label: '更换部件', required: false },
  { key: 'handling_result', label: '处理结果', required: false },
  { key: 'downtime_duration', label: '停机时长', required: false },
  { key: 'remarks', label: '备注', required: false },
]

function inferCategory(file) {
  const name = String(file?.name || '').toLowerCase()
  if (name.endsWith('.docx')) return 'work_order'
  if (name.endsWith('.xlsx')) return 'work_order'
  if (name.endsWith('.pdf')) return name.includes('维修') ? 'maintenance_record' : 'document'
  if (name.endsWith('.md') || name.endsWith('.txt')) return name.includes('维修') ? 'maintenance_record' : 'document'
  if (name.endsWith('.csv')) {
    if (name.includes('维修')) return 'maintenance_record'
    if (name.includes('工单')) return 'work_order'
    return ''
  }
  return ''
}

function buildDraft(item) {
  return {
    id: item.id,
    name: item.name,
    size: item.size,
    category: inferCategory(item.file),
    profile: null,
    profileStatus: 'idle',
    profileError: '',
    fieldMapping: {},
    documentConfig: {
      chunkSize: 800,
      skipEntity: false,
      skipRelation: false,
      syncToGenerateFta: true,
    },
    workOrderConfig: {
      fileId: '',
      syncToGenerateFta: true,
    },
    maintenanceConfig: {
      caseIdPrefix: 'case',
      maxSummaryChars: 800,
      skipEntity: false,
      skipRelation: false,
      syncToGenerateFta: true,
    },
  }
}

function getCategoryLabel(category) {
  return CATEGORY_OPTIONS.find((option) => option.value === category)?.label || '未选择'
}

function isDocxDraft(draft) {
  return String(draft?.name || '').toLowerCase().endsWith('.docx')
}

function isWorkOrderReady(draft) {
  if (isDocxDraft(draft)) return true
  const profile = draft?.profile
  if (!profile || draft?.profileStatus !== 'success') return false
  const mapping = draft?.fieldMapping || {}
  return WORK_ORDER_FIELDS.filter((field) => field.required).every((field) => String(mapping[field.key] || '').trim())
}

function isDraftReady(draft) {
  if (!draft?.category) return false
  if (draft.category === 'work_order') return isWorkOrderReady(draft)
  return true
}

export default function KnowledgeImportModal({
  open,
  items,
  busy = false,
  onClose,
  onConfirm,
  onProfileWorkOrder,
}) {
  const [drafts, setDrafts] = useState([])
  const [activeId, setActiveId] = useState('')
  const lastOpenRef = useRef(false)

  useEffect(() => {
    if (!open) {
      lastOpenRef.current = false
      return
    }
    if (lastOpenRef.current) return
    const nextDrafts = (items || []).map(buildDraft)
    setDrafts(nextDrafts)
    setActiveId(nextDrafts[0]?.id || '')
    lastOpenRef.current = true
  }, [open, items])

  const activeDraft = useMemo(
    () => drafts.find((draft) => draft.id === activeId) || drafts[0] || null,
    [drafts, activeId],
  )

  const updateDraft = (id, updater) => {
    setDrafts((prev) =>
      prev.map((draft) => {
        if (draft.id !== id) return draft
        return typeof updater === 'function' ? updater(draft) : { ...draft, ...updater }
      }),
    )
  }

  const handleCategoryChange = (id, category) => {
    updateDraft(id, (draft) => ({
      ...draft,
      category,
      profile: category === 'work_order' ? draft.profile : null,
      profileStatus: category === 'work_order' ? draft.profileStatus : 'idle',
      profileError: category === 'work_order' ? draft.profileError : '',
      fieldMapping: category === 'work_order' ? draft.fieldMapping : {},
    }))
  }

  const handleProfileWorkOrder = async (draft) => {
    if (!draft || !onProfileWorkOrder) return
    updateDraft(draft.id, { profileStatus: 'loading', profileError: '' })
    try {
      const fileItem = (items || []).find((item) => item.id === draft.id)
      const profile = await onProfileWorkOrder(fileItem?.file)
      updateDraft(draft.id, {
        profile,
        profileStatus: 'success',
        profileError: '',
        fieldMapping: profile?.recommended_field_mapping || {},
      })
    } catch (error) {
      updateDraft(draft.id, {
        profileStatus: 'failed',
        profileError: error?.message || '画像失败',
      })
    }
  }

  const allReady = drafts.length > 0 && drafts.every(isDraftReady)

  const handleConfirm = () => {
    if (!allReady || typeof onConfirm !== 'function') return
    const plans = drafts.map((draft) => ({
      id: draft.id,
      name: draft.name,
      category: draft.category,
      profile: draft.profile,
      fieldMapping: draft.fieldMapping,
      documentConfig: draft.documentConfig,
      workOrderConfig: draft.workOrderConfig,
      maintenanceConfig: draft.maintenanceConfig,
    }))
    onConfirm(plans)
  }

  if (!open) return null

  return (
    <div className="home-import-modal-overlay" role="presentation" onClick={busy ? undefined : onClose}>
      <div
        className="home-import-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="home-import-modal-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="home-import-modal-header">
          <div>
            <h2 className="home-import-modal-title" id="home-import-modal-title">
              知识导入向导
            </h2>
            <p className="home-import-modal-subtitle">
              先确认每个文件的业务类型，再走对应的 KB 导入接口。
            </p>
          </div>
          <button
            type="button"
            className="home-import-modal-close"
            onClick={busy ? undefined : onClose}
            disabled={busy}
            aria-label="关闭导入向导"
          >
            ×
          </button>
        </header>

        <div className="home-import-modal-body">
          <aside className="home-import-file-list">
            {drafts.map((draft) => (
              <button
                key={draft.id}
                type="button"
                className={`home-import-file-item${draft.id === activeDraft?.id ? ' home-import-file-item--active' : ''}`}
                onClick={() => setActiveId(draft.id)}
              >
                <div className="home-import-file-name" title={draft.name}>
                  {draft.name}
                </div>
                <div className="home-import-file-meta">
                  <span>{getCategoryLabel(draft.category)}</span>
                  <span>{(draft.size / 1024).toFixed(1)} KB</span>
                </div>
                {draft.profileStatus === 'loading' ? (
                  <div className="home-import-file-note">正在画像…</div>
                ) : null}
                {draft.profileStatus === 'failed' ? (
                  <div className="home-import-file-note home-import-file-note--error">画像失败</div>
                ) : null}
              </button>
            ))}
          </aside>

          <section className="home-import-config">
            {activeDraft ? (
              <>
                <div className="home-import-section">
                  <div className="home-import-label">文件</div>
                  <div className="home-import-file-title">{activeDraft.name}</div>
                </div>

                <div className="home-import-section">
                  <div className="home-import-label">数据类型</div>
                  <div className="home-import-category-grid">
                    {CATEGORY_OPTIONS.map((option) => (
                      <label
                        key={option.value}
                        className={`home-import-category-card${
                          activeDraft.category === option.value ? ' home-import-category-card--active' : ''
                        }`}
                      >
                        <input
                          type="radio"
                          name={`import-category-${activeDraft.id}`}
                          value={option.value}
                          checked={activeDraft.category === option.value}
                          onChange={() => handleCategoryChange(activeDraft.id, option.value)}
                        />
                        <strong>{option.label}</strong>
                        <span>{option.desc}</span>
                      </label>
                    ))}
                    <div className="home-import-category-card home-import-category-card--disabled">
                      <strong>时序数据</strong>
                      <span>本轮前端暂未开放，后端接口预留。</span>
                    </div>
                  </div>
                </div>

                {activeDraft.category === 'document' ? (
                  <div className="home-import-section">
                    <div className="home-import-label">文档导入配置</div>
                    <div className="home-import-form-grid">
                      <label>
                        <span>chunk 大小</span>
                        <input
                          type="number"
                          min="200"
                          max="4000"
                          value={activeDraft.documentConfig.chunkSize}
                          onChange={(e) =>
                            updateDraft(activeDraft.id, {
                              documentConfig: {
                                ...activeDraft.documentConfig,
                                chunkSize: Math.max(200, Number(e.target.value) || 800),
                              },
                            })
                          }
                        />
                      </label>
                    </div>
                    <div className="home-import-checkboxes">
                      <label>
                        <input
                          type="checkbox"
                          checked={activeDraft.documentConfig.skipEntity}
                          onChange={(e) =>
                            updateDraft(activeDraft.id, {
                              documentConfig: {
                                ...activeDraft.documentConfig,
                                skipEntity: e.target.checked,
                              },
                            })
                          }
                        />
                        跳过实体抽取
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          checked={activeDraft.documentConfig.skipRelation}
                          onChange={(e) =>
                            updateDraft(activeDraft.id, {
                              documentConfig: {
                                ...activeDraft.documentConfig,
                                skipRelation: e.target.checked,
                              },
                            })
                          }
                        />
                        跳过关系抽取
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          checked={activeDraft.documentConfig.syncToGenerateFta}
                          onChange={(e) =>
                            updateDraft(activeDraft.id, {
                              documentConfig: {
                                ...activeDraft.documentConfig,
                                syncToGenerateFta: e.target.checked,
                              },
                            })
                          }
                        />
                        导入后同步到 GNR
                      </label>
                    </div>
                  </div>
                ) : null}

                {activeDraft.category === 'work_order' ? (
                  <div className="home-import-section">
                    <div className="home-import-label">工单数据画像与映射</div>
                    <div className="home-import-inline-actions">
                      <button
                        type="button"
                        className="home-import-secondary-btn"
                        onClick={() => handleProfileWorkOrder(activeDraft)}
                        disabled={busy || activeDraft.profileStatus === 'loading'}
                      >
                        {activeDraft.profileStatus === 'loading' ? '识别中…' : '识别字段'}
                      </button>
                      {activeDraft.profile ? (
                        <span className="home-import-inline-note">
                          {activeDraft.profile.can_import ? '可导入' : '需要补全关键字段'}
                        </span>
                      ) : (
                        <span className="home-import-inline-note">CSV/XLSX 先画像并确认字段映射；DOCX 可直接按 PR 表单导入。</span>
                      )}
                    </div>

                    {activeDraft.profileError ? (
                      <div className="home-import-error">{activeDraft.profileError}</div>
                    ) : null}

                    {activeDraft.profile ? (
                      <>
                        <div className="home-import-profile-meta">
                          <span>检测类型：{activeDraft.profile.source_type || '—'}</span>
                          <span>行数：{activeDraft.profile.row_count ?? 0}</span>
                          <span>格式：{activeDraft.profile.file_format || '—'}</span>
                        </div>

                        {Array.isArray(activeDraft.profile.quality_issues) && activeDraft.profile.quality_issues.length ? (
                          <div className="home-import-warning-list">
                            {activeDraft.profile.quality_issues.map((issue, idx) => (
                              <div key={`${issue}-${idx}`}>{issue}</div>
                            ))}
                          </div>
                        ) : null}

                        <div className="home-import-mapping-table">
                          {WORK_ORDER_FIELDS.map((field) => (
                            <label key={field.key} className="home-import-mapping-row">
                              <span>
                                {field.label}
                                {field.required ? <em>必填</em> : null}
                              </span>
                              <select
                                value={activeDraft.fieldMapping?.[field.key] || ''}
                                onChange={(e) =>
                                  updateDraft(activeDraft.id, {
                                    fieldMapping: {
                                      ...(activeDraft.fieldMapping || {}),
                                      [field.key]: e.target.value,
                                    },
                                  })
                                }
                              >
                                <option value="">未映射</option>
                                {(activeDraft.profile.source_headers || []).map((header) => (
                                  <option key={header} value={header}>
                                    {header}
                                  </option>
                                ))}
                              </select>
                            </label>
                          ))}
                        </div>

                        <div className="home-import-checkboxes">
                          <label>
                            <input
                              type="checkbox"
                              checked={activeDraft.workOrderConfig.syncToGenerateFta}
                              onChange={(e) =>
                                updateDraft(activeDraft.id, {
                                  workOrderConfig: {
                                    ...activeDraft.workOrderConfig,
                                    syncToGenerateFta: e.target.checked,
                                  },
                                })
                              }
                            />
                            导入后同步到 GNR
                          </label>
                        </div>
                      </>
                    ) : null}
                  </div>
                ) : null}

                {activeDraft.category === 'maintenance_record' ? (
                  <div className="home-import-section">
                    <div className="home-import-label">维修记录导入配置</div>
                    <div className="home-import-form-grid">
                      <label>
                        <span>案例编号前缀</span>
                        <input
                          type="text"
                          value={activeDraft.maintenanceConfig.caseIdPrefix}
                          onChange={(e) =>
                            updateDraft(activeDraft.id, {
                              maintenanceConfig: {
                                ...activeDraft.maintenanceConfig,
                                caseIdPrefix: e.target.value,
                              },
                            })
                          }
                        />
                      </label>
                      <label>
                        <span>摘要最大长度</span>
                        <input
                          type="number"
                          min="120"
                          max="4000"
                          value={activeDraft.maintenanceConfig.maxSummaryChars}
                          onChange={(e) =>
                            updateDraft(activeDraft.id, {
                              maintenanceConfig: {
                                ...activeDraft.maintenanceConfig,
                                maxSummaryChars: Math.max(120, Number(e.target.value) || 800),
                              },
                            })
                          }
                        />
                      </label>
                    </div>
                    <div className="home-import-checkboxes">
                      <label>
                        <input
                          type="checkbox"
                          checked={activeDraft.maintenanceConfig.skipEntity}
                          onChange={(e) =>
                            updateDraft(activeDraft.id, {
                              maintenanceConfig: {
                                ...activeDraft.maintenanceConfig,
                                skipEntity: e.target.checked,
                              },
                            })
                          }
                        />
                        跳过实体抽取
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          checked={activeDraft.maintenanceConfig.skipRelation}
                          onChange={(e) =>
                            updateDraft(activeDraft.id, {
                              maintenanceConfig: {
                                ...activeDraft.maintenanceConfig,
                                skipRelation: e.target.checked,
                              },
                            })
                          }
                        />
                        跳过关系抽取
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          checked={activeDraft.maintenanceConfig.syncToGenerateFta}
                          onChange={(e) =>
                            updateDraft(activeDraft.id, {
                              maintenanceConfig: {
                                ...activeDraft.maintenanceConfig,
                                syncToGenerateFta: e.target.checked,
                              },
                            })
                          }
                        />
                        导入后同步到 GNR
                      </label>
                    </div>
                    <div className="home-import-hint">
                      当前文件将按维修案例切分，并生成 `maintenance_cases` 与 `case_summary` 证据块。
                    </div>
                  </div>
                ) : null}
              </>
            ) : (
              <div className="home-import-empty">暂无待导入文件。</div>
            )}
          </section>
        </div>

        <footer className="home-import-modal-footer">
          <div className="home-import-footer-note">
            {allReady ? '所有文件已完成配置，可以开始导入。' : '请先完成每个文件的数据类型选择与必要配置。'}
          </div>
          <div className="home-import-footer-actions">
            <button type="button" className="home-import-secondary-btn" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button
              type="button"
              className="home-send-btn"
              onClick={handleConfirm}
              disabled={busy || !allReady}
            >
              {busy ? '导入中…' : '开始导入'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}
