import { useEffect, useRef, useState } from 'react'

export default function SaveDescriptionModal({
  initialValue = '',
  onConfirm,
  onCancel,
}) {
  const [value, setValue] = useState(initialValue)
  const inputRef = useRef(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  return (
    <div className="fta-modal-overlay" onClick={onCancel}>
      <div className="fta-modal" onClick={(e) => e.stopPropagation()}>
        <div className="fta-modal-title">提交说明（可选）</div>
        <p
          style={{
            margin: '0 0 0.6rem',
            fontSize: '0.82rem',
            color: 'var(--fta-text-subtle)',
          }}
        >
          若不填写，将默认保存为“前端保存”。
        </p>
        <textarea
          ref={inputRef}
          className="fta-modal-input"
          style={{ minHeight: '5.5em', resize: 'vertical' }}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="例如：修正某节点类型、补充文档溯源…（可不填）"
        />
        <div className="fta-modal-actions">
          <button className="fta-btn ghost" onClick={onCancel}>
            取消
          </button>
          <button className="fta-btn primary" onClick={() => onConfirm?.(value)}>
            提交
          </button>
        </div>
      </div>
    </div>
  )
}

