import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import ExplodedViewer from '../components/ExplodedViewer.jsx'
import ThemeToggle from '../components/ThemeToggle.jsx'
import { IconChevronLeft } from '../components/icons.jsx'

function ExplodedPage() {
  const navigate = useNavigate()
  const [model, setModel] = useState('maglite')

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          padding: '16px 18px',
          borderBottom: '1px solid var(--border, rgba(255,255,255,0.08))',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <button
            type="button"
            onClick={() => navigate('/')}
            title="返回"
            aria-label="返回"
            style={{
              width: 38,
              height: 38,
              display: 'grid',
              placeItems: 'center',
              borderRadius: 12,
              border: '1px solid var(--border, rgba(255,255,255,0.08))',
              background: 'transparent',
              color: 'inherit',
              cursor: 'pointer',
            }}
          >
            <IconChevronLeft />
          </button>
          <div>
            <div style={{ fontWeight: 700, fontSize: 18 }}>爆炸图 3D 预览</div>
            <div style={{ opacity: 0.75, fontSize: 13 }}>
              拖拽旋转，右键平移，滚轮缩放
            </div>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <ThemeToggle />
        </div>
      </div>

      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '12px 18px',
          borderBottom: '1px solid var(--border, rgba(255,255,255,0.08))',
          flexWrap: 'wrap',
        }}
      >
        <button
          type="button"
          onClick={() => setModel('maglite')}
          style={{
            padding: '8px 12px',
            borderRadius: 12,
            border: '1px solid var(--border, rgba(255,255,255,0.08))',
            background: model === 'maglite' ? 'rgba(56, 189, 248, 0.16)' : 'transparent',
            color: 'inherit',
            cursor: 'pointer',
          }}
        >
          手电筒（maglite）
        </button>
        <button
          type="button"
          onClick={() => setModel('din-rail-box')}
          style={{
            padding: '8px 12px',
            borderRadius: 12,
            border: '1px solid var(--border, rgba(255,255,255,0.08))',
            background:
              model === 'din-rail-box' ? 'rgba(56, 189, 248, 0.16)' : 'transparent',
            color: 'inherit',
            cursor: 'pointer',
          }}
        >
          导轨盒（din-rail-box）
        </button>
      </div>

      <div style={{ flex: 1, minHeight: 0, padding: 18 }}>
        <div
          style={{
            width: '100%',
            height: '100%',
            borderRadius: 16,
            overflow: 'hidden',
            border: '1px solid var(--border, rgba(255,255,255,0.08))',
            background: 'rgba(0,0,0,0.12)',
          }}
        >
          <ExplodedViewer model={model} />
        </div>
      </div>
    </div>
  )
}

export default ExplodedPage

