import { useEffect, useMemo, useRef, useState } from 'react'

function formatVersionDate(createdAt) {
  if (createdAt == null) return '—'
  if (typeof createdAt === 'string') {
    const d = new Date(createdAt)
    return Number.isNaN(d.getTime()) ? createdAt : d.toLocaleString()
  }
  if (typeof createdAt === 'object' && createdAt.$date != null) {
    return new Date(createdAt.$date).toLocaleString()
  }
  return '—'
}

export default function VersionSelect({
  id = 'fta-version-select',
  value,
  options = [],
  disabled = false,
  onChange,
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef(null)

  const selected = useMemo(() => {
    const v = Number(value)
    return options.find((o) => Number(o?.version) === v) || null
  }, [options, value])

  useEffect(() => {
    if (!open) return undefined
    const onDoc = (e) => {
      if (!wrapRef.current) return
      if (!wrapRef.current.contains(e.target)) setOpen(false)
    }
    const onKey = (e) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const renderBadges = (row) => (
    <span className="fta-meta-badges fta-meta-badges--inline">
      <span className="fta-meta-badge fta-meta-badge--version">v{row?.version ?? '—'}</span>
      <span className="fta-meta-badge fta-meta-badge--editor">
        {row?.editor || '—'}
      </span>
      <span className="fta-meta-badge fta-meta-badge--time">{formatVersionDate(row?.created_at)}</span>
      <span className="fta-meta-badge fta-meta-badge--desc">{row?.description || '—'}</span>
    </span>
  )

  return (
    <div className="fta-version-select-wrap" ref={wrapRef}>
      <button
        id={id}
        type="button"
        className="fta-version-select-btn"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {selected ? renderBadges(selected) : <span style={{ opacity: 0.75 }}>请选择版本…</span>}
        <span className="fta-version-caret" aria-hidden>
          ▾
        </span>
      </button>

      {open && (
        <div className="fta-version-menu" role="listbox" aria-label="切换版本">
          {options.map((row) => {
            const isActive = Number(row?.version) === Number(value)
            return (
              <button
                key={row.version}
                type="button"
                role="option"
                aria-selected={isActive}
                className={`fta-version-option${isActive ? ' is-active' : ''}`}
                onClick={() => {
                  setOpen(false)
                  onChange?.(Number(row.version))
                }}
              >
                {renderBadges(row)}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

