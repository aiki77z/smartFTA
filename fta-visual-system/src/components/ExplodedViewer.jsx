import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Canvas, useThree } from '@react-three/fiber'
import { Bounds, OrbitControls, Stage, useBounds, useGLTF } from '@react-three/drei'

const MAGLITE_URL = new URL('../../exploded/maglite_-_exploded_view.glb', import.meta.url).href
const DIN_RAIL_BOX_URL = new URL(
  '../../exploded/microbd-din_rail_box_1-xc-exploded-view.glb',
  import.meta.url,
).href

// 默认映射（未上传自定义部件列表时使用）
const DEFAULT_PART_DETAILS = {
  // 导轨盒（MicroBd DIN rail box）——来自 glb 内 Mesh 名称
  Object_2: { id: 'BUTTON_BLACK', name: '功能按键（黑）', type: 'ui' },
  Object_3: { id: 'DIN_RAIL_BASE', name: '导轨底座', type: 'mechanical' },
  Object_4: { id: 'CAPACITOR_BLUE', name: '高压滤波电容', type: 'component' },
  Object_5: { id: 'ENCLOSURE_BODY', name: '外壳主体', type: 'mechanical' },
  Object_6: { id: 'IC_PACKAGE', name: '主控芯片封装', type: 'electronics' },
  Object_7: { id: 'LCD_BEZEL', name: '显示屏边框', type: 'ui' },
  Object_8: { id: 'FRONT_PANEL', name: '前面板与按键区', type: 'ui' },
  Object_9: { id: 'FASTENERS_A', name: '紧固件 A（螺钉/垫片）', type: 'mechanical' },
  Object_10: { id: 'FASTENERS_B', name: '紧固件 B（螺钉/垫片）', type: 'mechanical' },
  Object_11: { id: 'SMD_CAPACITOR', name: '贴片电容', type: 'electronics' },
  Object_12: { id: 'DISPLAY_MODULE', name: '显示模组（玻璃/背光）', type: 'ui' },
  Object_13: { id: 'INTERNAL_FRAME', name: '内部支架/结构件', type: 'mechanical' },
  Object_14: { id: 'MAIN_PCB', name: '主控电路板', type: 'electronics' },
  Object_15: { id: 'TERMINAL_BLOCK', name: '电源接线端子', type: 'connector' },
  Object_16: { id: 'RELAY_MODULES', name: '继电器模块', type: 'electronics' },
  Object_17: { id: 'SIX_PIN_TERMINALS', name: '6 针端子排', type: 'connector' },
  Object_18: { id: 'SOLDER_JOINTS', name: '焊点/焊锡', type: 'electronics' },
  Object_19: { id: 'TERMINAL_PINS_SCREWS', name: '端子针脚与压线螺钉', type: 'connector' },
  Object_20: { id: 'TERMINAL_COVERS', name: '端子保护盖', type: 'mechanical' },
  Object_21: { id: 'CRYSTAL_OSC', name: '晶振', type: 'electronics' },
  Object_22: { id: 'DIN_RAIL_CLIP', name: '导轨卡扣', type: 'mechanical' },
  Object_23: { id: 'BACK_COVER', name: '后盖/背板', type: 'mechanical' },
  Object_24: { id: 'LEGS_STANDOFFS', name: '支脚/绝缘支撑柱', type: 'mechanical' },
}

// 针对导轨盒 Object_x 编写的伪数据（后续可接入真实文档/维修记录/故障树映射）
const ASSET_METADATA = {
  MAIN_PCB: {
    name: '工业级主控制板 V2.1',
    specs: 'MCU: STM32H7, 供电: 24V DC, 通信: RS-485',
    manual_url: '/docs/pcb_manual.pdf',
    maintenance_history: [
      { date: '2025-12-10', action: '固件升级', technician: '张工' },
      { date: '2026-02-15', action: '热成像检测', status: '正常' },
    ],
    fault_tree_node: 'FTA_ROOT_002',
  },
  CAPACITOR_BLUE: {
    name: '450V 100uF 电解电容',
    manufacturer: 'Rubycon',
    expected_life: '5000 hours',
    common_faults: '鼓包、电解液泄露、ESR 增大',
    fault_tree_node: 'FTA_BASIC_014',
  },
  TERMINAL_BLOCK: {
    name: '电源接线端子（2P/3P 可选）',
    specs: '额定电流: 10A, 额定电压: 300V, 螺钉压线',
    inspection: '建议每 6 个月复紧一次端子螺钉',
    common_faults: '接触电阻增大、压线松动、端子发热变色',
    fault_tree_node: 'FTA_BASIC_021',
  },
  RELAY_MODULES: {
    name: '继电器输出模块',
    specs: '触点: 250VAC/10A, 线圈: 24VDC',
    common_faults: '触点烧蚀、线圈开路、吸合抖动',
    fault_tree_node: 'FTA_INTER_008',
  },
  DIN_RAIL_CLIP: {
    name: '导轨卡扣机构',
    specs: '适配 35mm DIN 导轨',
    common_faults: '卡扣疲劳断裂、安装松动',
    fault_tree_node: 'FTA_BASIC_033',
  },
}

function getPartForNodeName(nodeName, partDetailsMap) {
  const table =
    partDetailsMap && typeof partDetailsMap === 'object' && Object.keys(partDetailsMap).length
      ? partDetailsMap
      : DEFAULT_PART_DETAILS
  const detail = table[nodeName]
  if (detail) return detail
  return { id: nodeName, name: nodeName, type: 'unknown' }
}

function PartMesh({ nodeName, mesh, baseMaterial, selected, onPick }) {
  const material = useMemo(() => {
    // clone material per mesh so emissive highlight is isolated
    return baseMaterial?.clone?.() || baseMaterial
  }, [baseMaterial])

  useEffect(() => {
    if (!material) return
    // MeshStandardMaterial / MeshPhysicalMaterial: emissive exists
    if ('emissive' in material) {
      /* eslint-disable react-hooks/immutability */
      if (selected) {
        material.emissive.set('#ff2d2d')
        material.emissiveIntensity = 0.9
      } else {
        material.emissive.set('#000000')
        material.emissiveIntensity = 0
      }
      material.needsUpdate = true
      /* eslint-enable react-hooks/immutability */
    }
  }, [material, selected])

  return (
    <mesh
      name={nodeName}
      geometry={mesh.geometry}
      material={material}
      castShadow
      receiveShadow
      onClick={(e) => {
        e.stopPropagation()
        onPick(nodeName)
      }}
    />
  )
}

function DinRailModel({ url, selectedNodeName, onPick }) {
  const { nodes, materials } = useGLTF(url)

  const meshEntries = useMemo(() => {
    return Object.entries(nodes).filter(([, n]) => n && n.geometry)
  }, [nodes])

  // 这个模型是从 gltfjsx 输出看出来需要整体旋转 -90° 才“立起来”
  return (
    <group rotation={[-Math.PI / 2, 0, 0]}>
      {meshEntries.map(([nodeName, n]) => {
        // 尽量使用 mesh 自带 material，否则回退到 gltf materials 表
        const baseMat = n.material || materials?.[n.material?.name] || materials?.material || null
        return (
          <PartMesh
            key={nodeName}
            nodeName={nodeName}
            mesh={n}
            baseMaterial={baseMat}
            selected={selectedNodeName === nodeName}
            onPick={onPick}
          />
        )
      })}
    </group>
  )
}

function MagliteModel({ url, selectedNodeName, onPick }) {
  const { nodes } = useGLTF(url)
  const meshEntries = useMemo(() => Object.entries(nodes).filter(([, n]) => n && n.geometry), [nodes])
  return (
    <group>
      {meshEntries.map(([nodeName, n]) => (
        <PartMesh
          key={nodeName}
          nodeName={nodeName}
          mesh={n}
          baseMaterial={n.material}
          selected={selectedNodeName === nodeName}
          onPick={onPick}
        />
      ))}
    </group>
  )
}

function CameraRig({ controlsRef, cameraPreset, disableControls }) {
  const { camera } = useThree()

  useEffect(() => {
    if (!cameraPreset) return undefined
    const position = cameraPreset.position
    const target = cameraPreset.target
    const fov = cameraPreset.fov
    if (Array.isArray(position) && position.length === 3) {
      camera.position.set(position[0], position[1], position[2])
    }
    if (typeof fov === 'number' && Number.isFinite(fov)) {
      camera.fov = fov
      camera.updateProjectionMatrix()
    }
    if (Array.isArray(target) && target.length === 3) {
      controlsRef.current?.target?.set(target[0], target[1], target[2])
    }
    controlsRef.current?.update?.()
    return undefined
  }, [camera, cameraPreset, controlsRef])

  useEffect(() => {
    if (!disableControls) return undefined
    if (!controlsRef.current) return undefined
    controlsRef.current.enabled = false
    return () => {
      if (controlsRef.current) controlsRef.current.enabled = true
    }
  }, [controlsRef, disableControls])

  return null
}

function AutoFrame({ enabled, frameKey }) {
  const bounds = useBounds()
  useEffect(() => {
    if (!enabled) return undefined
    // refresh() must run after children mount; useBounds handles the internal timing.
    bounds.refresh().clip().fit()
    return undefined
  }, [bounds, enabled, frameKey])
  return null
}

export default function ExplodedViewer({
  model = 'din-rail-box',
  /** 用户上传的 GLB（blob/object URL）；与 partDetails 同时提供时覆盖内置模型 */
  customGlbUrl = '',
  /** 与 GLB 网格名对应的部件列表，格式见 DEFAULT_PART_DETAILS */
  partDetails = null,
  onPartClick,
  highlightNodeName = '',
  /** 画布每次选中事件节点时递增，用于同一 Object_X 或需强制刷新高亮时同步 */
  highlightSync = 0,
  /** 片头演示用：隐藏左上角 HUD */
  showHud = true,
  /** 片头演示用：固定相机视角（position/target/fov） */
  cameraPreset = null,
  /** 片头演示用：禁用鼠标交互，便于录屏 */
  disableControls = false,
  /** 片头演示用：自动旋转 */
  autoRotate = false,
  autoRotateSpeed = 0.7,
  /** 渲染质量：balanced | low（片头录屏建议 low，避免卡死） */
  quality = 'balanced',
  /** 自动居中取景：根据包围盒调整相机与 target */
  autoFrame = false,
  /** 外部触发重新取景（例如切换模型/分镜） */
  autoFrameKey = 0,
  autoFrameMargin = 1.18,
}) {
  const url = useMemo(() => {
    if (customGlbUrl) return customGlbUrl
    if (model === 'maglite') return MAGLITE_URL
    return DIN_RAIL_BOX_URL
  }, [model, customGlbUrl])

  const useMagliteLayout = !customGlbUrl && model === 'maglite'

  const [selectedNodeName, setSelectedNodeName] = useState('')

  useEffect(() => {
    setSelectedNodeName(highlightNodeName || '')
  }, [highlightNodeName, highlightSync])
  const selectedDetail = useMemo(() => {
    if (!selectedNodeName) return null
    return getPartForNodeName(selectedNodeName, partDetails)
  }, [selectedNodeName, partDetails])
  const handlePick = useCallback(
    (nodeName) => {
      setSelectedNodeName((prev) => (prev === nodeName ? '' : nodeName))
      const detail = getPartForNodeName(nodeName, partDetails)
      onPartClick?.({ nodeName, ...detail, metadata: ASSET_METADATA[detail.id] || null })
    },
    [onPartClick, partDetails],
  )

  useMemo(() => {
    useGLTF.preload(MAGLITE_URL)
    useGLTF.preload(DIN_RAIL_BOX_URL)
    return null
  }, [])

  const controlsRef = useRef(null)
  const lowQuality = quality === 'low'

  return (
    <div style={{ width: '100%', height: '100%', position: 'relative' }}>
      {showHud ? (
        <div
          style={{
            position: 'absolute',
            left: 12,
            top: 12,
            zIndex: 2,
            pointerEvents: 'none',
            maxWidth: 'min(360px, 72%)',
          }}
        >
          <div
            style={{
              pointerEvents: 'none',
              padding: '10px 12px',
              borderRadius: 12,
              border: '1px solid rgba(255,255,255,0.14)',
              background: 'rgba(2, 6, 23, 0.58)',
              color: '#e5e7eb',
              boxShadow: '0 18px 45px rgba(0,0,0,0.35)',
              backdropFilter: 'blur(6px)',
              fontSize: 12,
              lineHeight: 1.45,
            }}
          >
            {selectedDetail ? (
              <>
                <div style={{ fontWeight: 800, fontSize: 13, marginBottom: 4 }}>
                  {selectedDetail.name}
                </div>
                <div style={{ opacity: 0.92 }}>
                  <span style={{ opacity: 0.72 }}>id：</span>
                  <span style={{ fontFamily: 'ui-monospace, Menlo, Monaco, Consolas, monospace' }}>
                    {selectedDetail.id}
                  </span>
                </div>
                <div style={{ opacity: 0.92 }}>
                  <span style={{ opacity: 0.72 }}>type：</span>
                  <span style={{ fontFamily: 'ui-monospace, Menlo, Monaco, Consolas, monospace' }}>
                    {selectedDetail.type}
                  </span>
                </div>
              </>
            ) : (
              <div style={{ opacity: 0.9 }}>点击任意零件以查看部件信息（id / name / type）。</div>
            )}
          </div>
        </div>
      ) : null}
      <Canvas
        shadows={!lowQuality}
        dpr={lowQuality ? 1 : [1, 2]}
        style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block' }}
        camera={{ position: [2.2, 1.6, 2.2], fov: 45, near: 0.01, far: 200 }}
      >
        <CameraRig controlsRef={controlsRef} cameraPreset={cameraPreset} disableControls={disableControls} />
        <Suspense fallback={null}>
          <Bounds fit clip observe margin={autoFrame ? autoFrameMargin : 1.0}>
            <AutoFrame enabled={autoFrame && !cameraPreset} frameKey={autoFrameKey} boundsMargin={autoFrameMargin} />
            {lowQuality ? (
              <>
                <ambientLight intensity={0.85} />
                <directionalLight position={[4, 6, 4]} intensity={1.35} />
                <directionalLight position={[-5, 2, -3]} intensity={0.55} />
                {useMagliteLayout ? (
                  <MagliteModel url={url} selectedNodeName={selectedNodeName} onPick={handlePick} />
                ) : (
                  <DinRailModel url={url} selectedNodeName={selectedNodeName} onPick={handlePick} />
                )}
              </>
            ) : (
              <Stage environment="city" shadows>
                {useMagliteLayout ? (
                  <MagliteModel url={url} selectedNodeName={selectedNodeName} onPick={handlePick} />
                ) : (
                  <DinRailModel url={url} selectedNodeName={selectedNodeName} onPick={handlePick} />
                )}
              </Stage>
            )}
          </Bounds>
        </Suspense>
        <mesh
          onClick={() => setSelectedNodeName('')}
          visible={false}
        />
        <OrbitControls
          ref={controlsRef}
          makeDefault
          enableDamping
          dampingFactor={0.08}
          autoRotate={autoRotate}
          autoRotateSpeed={autoRotateSpeed}
        />
      </Canvas>
    </div>
  )
}

