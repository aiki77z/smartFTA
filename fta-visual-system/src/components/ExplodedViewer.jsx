import { Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { Canvas } from '@react-three/fiber'
import { OrbitControls, Stage, useGLTF } from '@react-three/drei'

const MAGLITE_URL = new URL('../../exploded/maglite_-_exploded_view.glb', import.meta.url).href
const DIN_RAIL_BOX_URL = new URL(
  '../../exploded/microbd-din_rail_box_1-xc-exploded-view.glb',
  import.meta.url,
).href

// 建议在外部定义映射表，方便后续构建故障树数据
export const PART_DETAILS = {
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
export const ASSET_METADATA = {
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

function getPartForNodeName(nodeName) {
  const detail = PART_DETAILS[nodeName]
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
      if (selected) {
        material.emissive.set('#ff2d2d')
        material.emissiveIntensity = 0.9
      } else {
        material.emissive.set('#000000')
        material.emissiveIntensity = 0
      }
      material.needsUpdate = true
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
        const detail = getPartForNodeName(nodeName)
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

export default function ExplodedViewer({ model = 'din-rail-box', onPartClick }) {
  const url = useMemo(() => {
    if (model === 'maglite') return MAGLITE_URL
    return DIN_RAIL_BOX_URL
  }, [model])

  const [selectedNodeName, setSelectedNodeName] = useState('')
  const selectedDetail = useMemo(() => {
    if (!selectedNodeName) return null
    return getPartForNodeName(selectedNodeName)
  }, [selectedNodeName])
  const handlePick = useCallback(
    (nodeName) => {
      setSelectedNodeName((prev) => (prev === nodeName ? '' : nodeName))
      const detail = getPartForNodeName(nodeName)
      onPartClick?.({ nodeName, ...detail, metadata: ASSET_METADATA[detail.id] || null })
    },
    [onPartClick],
  )

  useMemo(() => {
    useGLTF.preload(MAGLITE_URL)
    useGLTF.preload(DIN_RAIL_BOX_URL)
    return null
  }, [])

  return (
    <div style={{ width: '100%', height: '100%', position: 'relative' }}>
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
      <Canvas
        shadows
        dpr={[1, 2]}
        style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block' }}
        camera={{ position: [2.2, 1.6, 2.2], fov: 45, near: 0.01, far: 200 }}
      >
        <Suspense fallback={null}>
          <Stage environment="city" shadows>
            {model === 'maglite' ? (
              <MagliteModel
                url={url}
                selectedNodeName={selectedNodeName}
                onPick={handlePick}
              />
            ) : (
              <DinRailModel
                url={url}
                selectedNodeName={selectedNodeName}
                onPick={handlePick}
              />
            )}
          </Stage>
        </Suspense>
        <mesh
          onClick={() => setSelectedNodeName('')}
          visible={false}
        />
        <OrbitControls makeDefault enableDamping dampingFactor={0.08} />
      </Canvas>
    </div>
  )
}

