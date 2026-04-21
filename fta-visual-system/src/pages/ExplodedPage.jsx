import { Component, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import ExplodedViewer from '../components/ExplodedViewer.jsx'
import ThemeToggle from '../components/ThemeToggle.jsx'
import { IconChevronLeft } from '../components/icons.jsx'

const MICROBD_EXPLODED_URL = new URL('../../exploded/microbd-din_rail_box_1-xc-exploded-view.glb', import.meta.url)
  .href
// 组装态 GLB（录片头用）：放到 `fta-visual-system/public/demo/`，页面会自动探测是否存在
// 例如：`fta-visual-system/public/demo/microbd-din-rail-box-assembled.glb`
const MICROBD_ASSEMBLED_PUBLIC_URL = '/demo/microbd-din-rail-box-assembled.glb'

function useBoolParam(searchParams, key, fallback = false) {
  const raw = (searchParams.get(key) || '').trim().toLowerCase()
  if (!raw) return fallback
  if (raw === '1' || raw === 'true' || raw === 'yes' || raw === 'y' || raw === 'on') return true
  if (raw === '0' || raw === 'false' || raw === 'no' || raw === 'n' || raw === 'off') return false
  return fallback
}

function clamp01(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return 0
  return Math.max(0, Math.min(1, n))
}

// eslint-disable-next-line react/no-multi-comp
class Boundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }
  static getDerivedStateFromError(error) {
    return { error }
  }
  componentDidCatch() {}
  render() {
    if (this.state.error) {
      const msg = this.state.error?.message || String(this.state.error)
      return (
        <div
          style={{
            height: '100vh',
            display: 'grid',
            placeItems: 'center',
            padding: 24,
            background: 'radial-gradient(900px 500px at 20% 20%, rgba(56,189,248,0.10), rgba(0,0,0,0) 60%), #050816',
            color: 'rgba(248,250,252,0.95)',
          }}
        >
          <div style={{ maxWidth: 880, width: '100%' }}>
            <div style={{ fontWeight: 900, fontSize: 22, marginBottom: 10 }}>页面运行时错误（已拦截，避免白屏）</div>
            <div style={{ opacity: 0.9, marginBottom: 10 }}>
              请把这段报错发我，我会按行定位修复。
            </div>
            <pre
              style={{
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                padding: 14,
                borderRadius: 14,
                border: '1px solid rgba(255,255,255,0.12)',
                background: 'rgba(2,6,23,0.62)',
              }}
            >
              {msg}
            </pre>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

function ExplodedPage() {
  const navigate = useNavigate()
  const [model, setModel] = useState('maglite')
  const [searchParams] = useSearchParams()

  const scene = (searchParams.get('scene') || '').trim().toLowerCase()
  const introMode = scene === 'intro'
  const autoplay = useBoolParam(searchParams, 'autoplay', true)

  const [introHidden, setIntroHidden] = useState(false)
  const [introPaused, setIntroPaused] = useState(!autoplay)
  const [introStep, setIntroStep] = useState(0)
  const [highlightNodeName, setHighlightNodeName] = useState('')
  const [highlightSync, setHighlightSync] = useState(0)
  const [useExplodedModel, setUseExplodedModel] = useState(false)
  const [assembledUrlAvailable, setAssembledUrlAvailable] = useState(false)
  const [assembledUrlWithVersion, setAssembledUrlWithVersion] = useState(MICROBD_ASSEMBLED_PUBLIC_URL)
  const [fatalError, setFatalError] = useState('')
  const [autoFrameKey, setAutoFrameKey] = useState(0)

  const stepTimerRef = useRef(0)

  useEffect(() => {
    if (!introMode) return undefined
    // 避免浏览器/three 缓存导致“同 URL 读到旧的 index.html”
    const version = Date.now()
    const versionedUrl = `${MICROBD_ASSEMBLED_PUBLIC_URL}?v=${version}`
    setAssembledUrlWithVersion(versionedUrl)
    let cancelled = false
    ;(async () => {
      try {
        // Vite dev server 可能对不存在的静态资源返回 SPA HTML（甚至 200），HEAD 不可靠。
        // 这里用 Range 拉取前 4 字节并校验 GLB 魔数：`glTF`
        const res = await fetch(versionedUrl, {
          method: 'GET',
          headers: { Range: 'bytes=0-3' },
        })
        if (!res || !res.ok) {
          if (!cancelled) setAssembledUrlAvailable(false)
          return
        }
        const buf = await res.arrayBuffer()
        const u8 = new Uint8Array(buf)
        const magicOk =
          u8.length >= 4 && u8[0] === 0x67 /* g */ && u8[1] === 0x6c /* l */ && u8[2] === 0x54 /* T */ && u8[3] === 0x46 /* F */
        if (!cancelled) setAssembledUrlAvailable(magicOk)
      } catch {
        if (!cancelled) setAssembledUrlAvailable(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [introMode])

  useEffect(() => {
    if (!introMode) return undefined
    const onErr = (e) => {
      const msg = e?.message || e?.error?.message || String(e)
      setFatalError(msg)
    }
    const onRej = (e) => {
      const reason = e?.reason
      const msg = reason?.message || String(reason || e)
      setFatalError(msg)
    }
    window.addEventListener('error', onErr)
    window.addEventListener('unhandledrejection', onRej)
    return () => {
      window.removeEventListener('error', onErr)
      window.removeEventListener('unhandledrejection', onRej)
    }
  }, [introMode])

  const introGlbUrl = useMemo(() => {
    if (useExplodedModel) return MICROBD_EXPLODED_URL
    return assembledUrlAvailable ? assembledUrlWithVersion : MICROBD_EXPLODED_URL
  }, [assembledUrlAvailable, assembledUrlWithVersion, useExplodedModel])

  const introCameraPreset = useMemo(() => {
    // 轻量“镜头”切换：通过 step 控制固定视角与焦点
    if (introStep <= 1) {
      return { position: [2.4, 1.35, 2.4], target: [0, 0.25, 0], fov: 40 }
    }
    if (introStep === 2) {
      return { position: [1.65, 1.15, 1.2], target: [0.05, 0.22, 0.05], fov: 38 }
    }
    if (introStep === 3) {
      return { position: [2.8, 1.35, 1.2], target: [0.0, 0.22, 0.05], fov: 42 }
    }
    return { position: [2.2, 1.55, 2.2], target: [0.0, 0.2, 0.0], fov: 45 }
  }, [introStep])

  const introLines = useMemo(() => {
    // 片头要“先定位核心问题”，避免太像功能演示：用一句话点出异常 + 一句引出知识驱动FTA
    const lines = [
      { k: 'scene', t: '现场：MicroBd 导轨式智能控制器', s: '设备上线后，操作人员反馈：人机交互异常。' },
      { k: 'symptom', t: '现象：按键无响应 / 显示异常', s: '肉眼可见，但故障原因可能分布在多条链路。' },
      { k: 'focus', t: '我们先锁定：人机交互链路', s: '从“UI部件 → 电路板 → 固件/通信”逐层拆解。' },
      { k: 'explode', t: '拆解：打开设备，定位关键部件', s: '爆炸图对齐物理组件，让故障事件可追溯、可验证。' },
      { k: 'cta', t: '进入：基于知识的故障树智能构建', s: 'AI 生成初稿，专家校验优化，快速得到可用故障树。' },
    ]
    return lines[Math.max(0, Math.min(lines.length - 1, introStep))]
  }, [introStep])

  useEffect(() => {
    if (!introMode) return undefined
    if (introPaused) return undefined

    window.clearTimeout(stepTimerRef.current)
    const ms =
      introStep === 0 ? 2400 : introStep === 1 ? 2400 : introStep === 2 ? 2600 : introStep === 3 ? 2600 : 2200
    stepTimerRef.current = window.setTimeout(() => {
      setIntroStep((s) => Math.min(4, s + 1))
    }, ms)
    return () => window.clearTimeout(stepTimerRef.current)
  }, [introMode, introPaused, introStep])

  useEffect(() => {
    if (!introMode) return
    // 分镜对模型与高亮的编排
    if (introStep <= 2) setUseExplodedModel(false)
    if (introStep >= 3) setUseExplodedModel(true)

    let node = ''
    if (introStep === 1) node = 'Object_2' // 功能按键（黑）
    if (introStep === 2) node = 'Object_12' // 显示模组
    if (introStep === 3) node = 'Object_14' // 主控电路板
    if (introStep === 4) node = '' // CTA 不高亮，避免喧宾夺主

    setHighlightNodeName(node)
    setHighlightSync((x) => x + 1)
    setAutoFrameKey((x) => x + 1)
  }, [introMode, introStep])

  useEffect(() => {
    if (!introMode) return undefined
    // 进入 intro 时重置一次，确保录制可复现
    setIntroStep(0)
    setIntroHidden(false)
    setIntroPaused(!autoplay)
    setUseExplodedModel(false)
    setHighlightNodeName('')
    setHighlightSync((x) => x + 1)
    return undefined
  }, [introMode, autoplay])

  if (introMode && fatalError) {
    return (
      <div
        style={{
          height: '100vh',
          display: 'grid',
          placeItems: 'center',
          padding: 24,
          background: 'radial-gradient(900px 500px at 20% 20%, rgba(56,189,248,0.10), rgba(0,0,0,0) 60%), #050816',
          color: 'rgba(248,250,252,0.95)',
        }}
      >
        <div style={{ maxWidth: 880, width: '100%' }}>
          <div style={{ fontWeight: 900, fontSize: 22, marginBottom: 10 }}>页面运行时错误（捕获到）</div>
          <div style={{ opacity: 0.9, marginBottom: 10 }}>
            请把下面这行错误信息发我（通常就是白屏原因）。
          </div>
          <pre
            style={{
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              padding: 14,
              borderRadius: 14,
              border: '1px solid rgba(255,255,255,0.12)',
              background: 'rgba(2,6,23,0.62)',
            }}
          >
            {fatalError}
          </pre>
          <button
            type="button"
            onClick={() => {
              setFatalError('')
              setIntroStep(0)
              setIntroHidden(false)
              setIntroPaused(false)
            }}
            style={{
              marginTop: 14,
              padding: '10px 12px',
              borderRadius: 14,
              border: '1px solid rgba(255,255,255,0.14)',
              background: 'rgba(2, 6, 23, 0.58)',
              color: 'rgba(248,250,252,0.95)',
              cursor: 'pointer',
              backdropFilter: 'blur(8px)',
            }}
          >
            重新尝试进入片头
          </button>
        </div>
      </div>
    )
  }

  return (
    <Boundary>
      <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          padding: '16px 18px',
          borderBottom: '1px solid var(--border, rgba(255,255,255,0.08))',
          ...(introMode
            ? {
                position: 'absolute',
                left: 0,
                top: 0,
                right: 0,
                zIndex: 20,
                background: 'linear-gradient(to bottom, rgba(2,6,23,0.72), rgba(2,6,23,0))',
                borderBottom: 'none',
                pointerEvents: 'none',
              }
            : null),
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
              ...(introMode ? { pointerEvents: 'auto' } : null),
            }}
          >
            <IconChevronLeft />
          </button>
          <div>
            <div style={{ fontWeight: 700, fontSize: 18 }}>
              {introMode ? '片头场景：人机交互异常' : '爆炸图 3D 预览'}
            </div>
            <div style={{ opacity: 0.75, fontSize: 13 }}>
              {introMode ? '用于录制演示视频开头的小片头' : '拖拽旋转，右键平移，滚轮缩放'}
            </div>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <ThemeToggle />
        </div>
      </div>

      {!introMode ? (
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
              background: model === 'din-rail-box' ? 'rgba(56, 189, 248, 0.16)' : 'transparent',
              color: 'inherit',
              cursor: 'pointer',
            }}
          >
            导轨盒（din-rail-box）
          </button>
        </div>
      ) : null}

      <div style={{ flex: 1, minHeight: 0, padding: introMode ? 0 : 18, position: 'relative' }}>
        <div
          style={{
            width: '100%',
            height: '100%',
            borderRadius: introMode ? 0 : 16,
            overflow: 'hidden',
            border: '1px solid var(--border, rgba(255,255,255,0.08))',
            background: 'rgba(0,0,0,0.12)',
          }}
        >
          {introMode ? (
            <ExplodedViewer
              model="din-rail-box"
              customGlbUrl={introGlbUrl}
              showHud={false}
              disableControls
              autoRotate={!introPaused}
              autoRotateSpeed={0.55}
              highlightNodeName={highlightNodeName}
              highlightSync={highlightSync}
              cameraPreset={null}
              quality="low"
              autoFrame
              autoFrameKey={autoFrameKey}
              autoFrameMargin={1.22}
            />
          ) : (
            <ExplodedViewer model={model} />
          )}
        </div>

        {introMode && !introHidden ? (
          <>
            <div
              style={{
                position: 'absolute',
                left: 0,
                right: 0,
                top: 0,
                bottom: 0,
                pointerEvents: 'none',
                background:
                  'radial-gradient(1200px 600px at 20% 20%, rgba(56,189,248,0.10), rgba(0,0,0,0) 60%), radial-gradient(900px 500px at 80% 30%, rgba(244,63,94,0.10), rgba(0,0,0,0) 65%)',
              }}
            />

            {/* Letterbox for cinematic feel */}
            <div
              style={{
                position: 'absolute',
                left: 0,
                right: 0,
                top: 0,
                height: '7.5vh',
                background: 'linear-gradient(to bottom, rgba(0,0,0,0.72), rgba(0,0,0,0))',
                pointerEvents: 'none',
              }}
            />
            <div
              style={{
                position: 'absolute',
                left: 0,
                right: 0,
                bottom: 0,
                height: '10vh',
                background: 'linear-gradient(to top, rgba(0,0,0,0.76), rgba(0,0,0,0))',
                pointerEvents: 'none',
              }}
            />

            <div
              style={{
                position: 'absolute',
                left: 'min(44px, 4vw)',
                right: 'min(44px, 4vw)',
                bottom: 'min(54px, 5.5vh)',
                pointerEvents: 'none',
                color: 'rgba(248,250,252,0.95)',
                textShadow: '0 10px 40px rgba(0,0,0,0.55)',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 12,
                  marginBottom: 10,
                }}
              >
                <div style={{ fontSize: 13, opacity: 0.82 }}>
                  片头分镜 {introStep + 1}/5 · {useExplodedModel ? '爆炸图' : '组装态'}
                </div>
                <div style={{ fontSize: 12, opacity: 0.82 }}>建议录制分辨率：1080p</div>
              </div>

              <div
                style={{
                  padding: '14px 16px',
                  borderRadius: 16,
                  border: '1px solid rgba(255,255,255,0.14)',
                  background: 'rgba(2, 6, 23, 0.52)',
                  backdropFilter: 'blur(8px)',
                  boxShadow: '0 18px 55px rgba(0,0,0,0.38)',
                }}
              >
                <div style={{ fontWeight: 900, letterSpacing: 0.2, fontSize: 22, lineHeight: 1.18 }}>
                  {introLines.t}
                </div>
                <div style={{ marginTop: 8, fontSize: 14, opacity: 0.9, lineHeight: 1.55 }}>
                  {introLines.s}
                </div>
                <div style={{ marginTop: 12, height: 3, borderRadius: 999, background: 'rgba(255,255,255,0.10)' }}>
                  <div
                    style={{
                      height: '100%',
                      width: `${Math.round(clamp01((introStep + 1) / 5) * 100)}%`,
                      borderRadius: 999,
                      background: 'linear-gradient(90deg, rgba(56,189,248,0.95), rgba(244,63,94,0.85))',
                      boxShadow: '0 6px 18px rgba(56,189,248,0.20)',
                    }}
                  />
                </div>
              </div>
            </div>

            {/* Control bar (pointer events enabled) */}
            <div
              style={{
                position: 'absolute',
                left: 18,
                bottom: 18,
                display: 'flex',
                gap: 10,
                zIndex: 30,
              }}
            >
              <button
                type="button"
                onClick={() => setIntroPaused((p) => !p)}
                style={{
                  pointerEvents: 'auto',
                  padding: '10px 12px',
                  borderRadius: 14,
                  border: '1px solid rgba(255,255,255,0.14)',
                  background: 'rgba(2, 6, 23, 0.58)',
                  color: 'rgba(248,250,252,0.95)',
                  cursor: 'pointer',
                  backdropFilter: 'blur(8px)',
                }}
              >
                {introPaused ? '继续播放' : '暂停'}
              </button>
              <button
                type="button"
                onClick={() => setIntroStep((s) => Math.min(4, s + 1))}
                style={{
                  pointerEvents: 'auto',
                  padding: '10px 12px',
                  borderRadius: 14,
                  border: '1px solid rgba(255,255,255,0.14)',
                  background: 'rgba(2, 6, 23, 0.58)',
                  color: 'rgba(248,250,252,0.95)',
                  cursor: 'pointer',
                  backdropFilter: 'blur(8px)',
                }}
              >
                下一镜
              </button>
              <button
                type="button"
                onClick={() => {
                  setIntroPaused(true)
                  setIntroHidden(true)
                }}
                style={{
                  pointerEvents: 'auto',
                  padding: '10px 12px',
                  borderRadius: 14,
                  border: '1px solid rgba(255,255,255,0.14)',
                  background: 'rgba(2, 6, 23, 0.58)',
                  color: 'rgba(248,250,252,0.95)',
                  cursor: 'pointer',
                  backdropFilter: 'blur(8px)',
                }}
                title="隐藏字幕与控制条，保留纯画面"
              >
                隐藏字幕
              </button>
              <button
                type="button"
                onClick={() => {
                  setIntroPaused(true)
                  setIntroStep(0)
                }}
                style={{
                  pointerEvents: 'auto',
                  padding: '10px 12px',
                  borderRadius: 14,
                  border: '1px solid rgba(255,255,255,0.14)',
                  background: 'rgba(2, 6, 23, 0.58)',
                  color: 'rgba(248,250,252,0.95)',
                  cursor: 'pointer',
                  backdropFilter: 'blur(8px)',
                }}
              >
                重置
              </button>
            </div>
          </>
        ) : null}

        {introMode && introHidden ? (
          <button
            type="button"
            onClick={() => setIntroHidden(false)}
            style={{
              position: 'absolute',
              left: 18,
              bottom: 18,
              zIndex: 30,
              padding: '10px 12px',
              borderRadius: 14,
              border: '1px solid rgba(255,255,255,0.14)',
              background: 'rgba(2, 6, 23, 0.58)',
              color: 'rgba(248,250,252,0.95)',
              cursor: 'pointer',
              backdropFilter: 'blur(8px)',
            }}
            title="恢复字幕与控制条"
          >
            显示字幕
          </button>
        ) : null}
      </div>
      </div>
    </Boundary>
  )
}

export default ExplodedPage

