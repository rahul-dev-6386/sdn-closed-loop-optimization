/**
 * Interactive 3D SDN Highway & Vehicle Traffic Visualization using Three.js.
 *
 * Renders switches, hosts, highway roadways, animated 3D cyber-vehicles,
 * glowing active paths, and pulsing congestion hazard zones in full 3D WebGL.
 */
import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import type { TopologyDef } from '../topologies'

export interface Topology3DProps {
  topologyDef: TopologyDef
  activePath: string[]
  bottleneckLink: string
  isCongested: boolean
  selected: string | null
  onSelect: (name: string | null) => void
  cameraPreset?: 'perspective' | 'top' | 'follow'
  autoRotate?: boolean
}

const W = 680
const H = 380
const PAD_X = 64
const PAD_Y = 56

function layoutTopology(topo: TopologyDef): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>()

  // 1. Layered deterministic coordinates from catalog
  if (topo.node_coordinates && Object.keys(topo.node_coordinates).length > 0) {
    for (const [name, coord] of Object.entries(topo.node_coordinates)) {
      positions.set(name, { x: coord.x, y: coord.y })
    }
    return positions
  }

  const switches = topo.switches
  if (!switches || switches.length === 0) return positions

  const neighbours = new Map<string, Set<string>>()
  for (const s of switches) neighbours.set(s, new Set())
  for (const link of topo.links) {
    if (link.host_link) continue
    neighbours.get(link.a)?.add(link.b)
    neighbours.get(link.b)?.add(link.a)
  }

  const root = switches[0]
  const depth = new Map<string, number>([[root, 0]])
  const queue = [root]
  while (queue.length) {
    const node = queue.shift()!
    for (const next of neighbours.get(node) ?? []) {
      if (!depth.has(next)) {
        depth.set(next, depth.get(node)! + 1)
        queue.push(next)
      }
    }
  }

  const maxDepth = Math.max(0, ...depth.values())
  for (const s of switches) if (!depth.has(s)) depth.set(s, maxDepth)

  const layers = new Map<number, string[]>()
  for (const s of switches) {
    const d = depth.get(s)!
    layers.set(d, [...(layers.get(d) ?? []), s])
  }

  const maxLayer = Math.max(...layers.keys())
  const usableW = W - PAD_X * 2
  const usableH = H - PAD_Y * 2

  for (const [d, members] of layers) {
    const x = PAD_X + (usableW * (maxLayer === 0 ? 0.5 : d / maxLayer))
    members.forEach((name, i) => {
      const offset =
        members.length === 1
          ? 0
          : (i - (members.length - 1) / 2) * (usableH / Math.max(2, members.length + 0.3))
      positions.set(name, { x, y: H / 2 + offset })
    })
  }

  const seen = new Map<string, number>()
  for (const host of topo.hosts) {
    const anchor = positions.get(host.switch)
    if (!anchor) continue
    const n = seen.get(host.switch) ?? 0
    seen.set(host.switch, n + 1)
    positions.set(host.name, {
      x: anchor.x + (anchor.x < W / 2 ? -48 : 48),
      y: anchor.y + (n % 2 === 0 ? -48 : 48),
    })
  }
  return positions
}

function makeTextSprite(
  text: string,
  color = '#e2e8f0',
  bgColor = 'rgba(15, 23, 42, 0.90)',
  borderColor = '#38bdf8'
): THREE.Sprite {
  const canvas = document.createElement('canvas')
  canvas.width = 384
  canvas.height = 96
  const ctx = canvas.getContext('2d')!

  ctx.fillStyle = bgColor
  ctx.strokeStyle = borderColor
  ctx.lineWidth = 4
  ctx.beginPath()
  ctx.roundRect(6, 6, 372, 84, 12)
  ctx.fill()
  ctx.stroke()

  ctx.font = 'bold 30px "IBM Plex Mono", monospace'
  ctx.fillStyle = color
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText(text, 192, 48)

  const texture = new THREE.CanvasTexture(canvas)
  texture.minFilter = THREE.LinearFilter
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false })
  const sprite = new THREE.Sprite(material)
  sprite.scale.set(3.4, 0.85, 1)
  return sprite
}

/** Procedural 3D futuristic car model */
function createVehicleMesh(color: string): THREE.Group {
  const car = new THREE.Group()

  // Main chassis
  const chassisGeo = new THREE.BoxGeometry(1.5, 0.38, 0.76)
  const chassisMat = new THREE.MeshStandardMaterial({
    color: new THREE.Color(color),
    metalness: 0.7,
    roughness: 0.25,
  })
  const chassis = new THREE.Mesh(chassisGeo, chassisMat)
  chassis.position.y = 0.24
  car.add(chassis)

  // Cabin / Windshield
  const cabinGeo = new THREE.BoxGeometry(0.78, 0.30, 0.62)
  const cabinMat = new THREE.MeshStandardMaterial({
    color: 0x0f172a,
    metalness: 0.9,
    roughness: 0.1,
  })
  const cabin = new THREE.Mesh(cabinGeo, cabinMat)
  cabin.position.set(-0.1, 0.48, 0)
  car.add(cabin)

  // 4 Wheels
  const wheelGeo = new THREE.CylinderGeometry(0.18, 0.18, 0.12, 14)
  const wheelMat = new THREE.MeshStandardMaterial({ color: 0x1e293b, roughness: 0.8 })
  wheelGeo.rotateZ(Math.PI / 2)

  const wheelPositions = [
    [-0.48, 0.18, -0.40],
    [-0.48, 0.18, 0.40],
    [0.48, 0.18, -0.40],
    [0.48, 0.18, 0.40],
  ]
  wheelPositions.forEach(([wx, wy, wz]) => {
    const w = new THREE.Mesh(wheelGeo, wheelMat)
    w.position.set(wx, wy, wz)
    car.add(w)
  })

  // 2 Headlights (forward facing in +X direction)
  const lightGeo = new THREE.BoxGeometry(0.08, 0.12, 0.16)
  const headMat = new THREE.MeshBasicMaterial({ color: 0xfef08a })
  const hl1 = new THREE.Mesh(lightGeo, headMat)
  hl1.position.set(0.76, 0.27, -0.24)
  const hl2 = new THREE.Mesh(lightGeo, headMat)
  hl2.position.set(0.76, 0.27, 0.24)
  car.add(hl1, hl2)

  // 2 Taillights (rear facing in -X direction)
  const tailMat = new THREE.MeshBasicMaterial({ color: 0xef4444 })
  const tl1 = new THREE.Mesh(lightGeo, tailMat)
  tl1.position.set(-0.76, 0.27, -0.24)
  const tl2 = new THREE.Mesh(lightGeo, tailMat)
  tl2.position.set(-0.76, 0.27, 0.24)
  car.add(tl1, tl2)

  return car
}

export function Topology3D({
  topologyDef,
  activePath,
  bottleneckLink,
  isCongested,
  selected,
  onSelect,
  cameraPreset = 'perspective',
  autoRotate = false,
}: Topology3DProps) {
  const mountRef = useRef<HTMLDivElement | null>(null)
  const controlsRef = useRef<OrbitControls | null>(null)
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null)
  const [hoveredNode, setHoveredNode] = useState<string | null>(null)

  useEffect(() => {
    const container = mountRef.current
    if (!container) return

    const width = container.clientWidth || 700
    const height = container.clientHeight || 450

    // 1. Scene & Environment
    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x0c1117)
    scene.fog = new THREE.FogExp2(0x0c1117, 0.015)

    // 2. Camera & Orbit Controls
    const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000)
    cameraRef.current = camera

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    renderer.setSize(width, height)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.shadowMap.enabled = true
    container.innerHTML = ''
    container.appendChild(renderer.domElement)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.05
    controls.maxDistance = 60
    controls.minDistance = 8
    controls.maxPolarAngle = Math.PI / 2 - 0.05
    controlsRef.current = controls

    // Calculate node coordinate layout normalized and centered around (0, 0, 0)
    const pos2d = layoutTopology(topologyDef)
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
    for (const p of pos2d.values()) {
      if (p.x < minX) minX = p.x
      if (p.x > maxX) maxX = p.x
      if (p.y < minY) minY = p.y
      if (p.y > maxY) maxY = p.y
    }
    const spanX = Math.max(1, maxX - minX)
    const spanY = Math.max(1, maxY - minY)
    const midX = (minX + maxX) / 2
    const midY = (minY + maxY) / 2

    const pos3d = new Map<string, THREE.Vector3>()
    for (const [name, p] of pos2d.entries()) {
      const x = ((p.x - midX) / spanX) * 19.5
      const z = ((p.y - midY) / spanY) * 9.5
      pos3d.set(name, new THREE.Vector3(x, 0, z))
    }

    // Camera initial position centered right on the network with mild 35° tilt
    camera.fov = 35
    camera.updateProjectionMatrix()
    if (cameraPreset === 'top') {
      camera.position.set(0, 36, 0.001)
      controls.target.set(0, 0, 0)
    } else if (cameraPreset === 'follow') {
      camera.position.set(0, 16, 18)
      controls.target.set(0, 0, 0)
    } else {
      // Mild tilt ~35° from vertical (narrow FOV, no fish-eye distortion)
      camera.position.set(0, 25, 17)
      controls.target.set(0, 0, 0)
    }
    controls.update()

    // 3. Cyber grid ground plane
    const grid = new THREE.GridHelper(50, 30, 0x38bdf8, 0x1e293b)
    grid.position.y = -0.05
    scene.add(grid)

    // 4. Lighting
    const ambientLight = new THREE.AmbientLight(0x94a3b8, 0.85)
    scene.add(ambientLight)

    const dirLight = new THREE.DirectionalLight(0xffffff, 1.3)
    dirLight.position.set(20, 35, 20)
    dirLight.castShadow = true
    scene.add(dirLight)

    // 5. Build Roadways between linked nodes
    const roadGroup = new THREE.Group()
    const hazardGroup = new THREE.Group()
    const activeRoadGroup = new THREE.Group()

    const activeLinkKeys = new Set<string>()
    for (let i = 0; i < activePath.length - 1; i++) {
      activeLinkKeys.add([activePath[i], activePath[i + 1]].sort().join('--'))
    }

    const asphaltMat = new THREE.MeshStandardMaterial({
      color: 0x182029,
      roughness: 0.75,
      metalness: 0.15,
    })
    const curbMat = new THREE.MeshStandardMaterial({
      color: 0x38bdf8,
      emissive: 0x0284c7,
      emissiveIntensity: 0.35,
      roughness: 0.4,
    })
    const laneMat = new THREE.MeshBasicMaterial({
      color: 0xfde047,
    })
    const activeLaserMat = new THREE.MeshStandardMaterial({
      color: 0x10b981,
      emissive: 0x10b981,
      emissiveIntensity: 0.9,
      roughness: 0.2,
    })
    const hazardLaserMat = new THREE.MeshStandardMaterial({
      color: 0xef4444,
      emissive: 0xdc2626,
      emissiveIntensity: 1.2,
      roughness: 0.2,
    })

    topologyDef.links.forEach((link) => {
      const p1 = pos3d.get(link.a)
      const p2 = pos3d.get(link.b)
      if (!p1 || !p2) return

      const linkKey = [link.a, link.b].sort().join('--')
      const isActive = activeLinkKeys.has(linkKey)
      const isBottleneck = linkKey === bottleneckLink

      // Link vector and distance
      const dir = new THREE.Vector3().subVectors(p2, p1)
      const length = dir.length()
      const mid = new THREE.Vector3().addVectors(p1, p2).multiplyScalar(0.5)

      // Road bed mesh
      const roadGeo = new THREE.BoxGeometry(1.3, 0.10, length)
      const roadMesh = new THREE.Mesh(roadGeo, asphaltMat)
      roadMesh.position.copy(mid)
      roadMesh.position.y = 0.08
      roadMesh.lookAt(p2)
      roadGroup.add(roadMesh)

      // Guardrails / Curbs with glowing cyan LED trim
      const curbGeo = new THREE.BoxGeometry(0.10, 0.16, length)
      const curbL = new THREE.Mesh(curbGeo, curbMat)
      curbL.position.copy(mid)
      curbL.position.y = 0.12
      curbL.lookAt(p2)
      curbL.translateX(-0.65)

      const curbR = new THREE.Mesh(curbGeo, curbMat)
      curbR.position.copy(mid)
      curbR.position.y = 0.12
      curbR.lookAt(p2)
      curbR.translateX(0.65)
      roadGroup.add(curbL, curbR)

      // Center dashed road lane
      const laneGeo = new THREE.BoxGeometry(0.06, 0.11, length * 0.88)
      const lane = new THREE.Mesh(laneGeo, laneMat)
      lane.position.copy(mid)
      lane.position.y = 0.10
      lane.lookAt(p2)
      roadGroup.add(lane)

      // Highlight active highway lane
      if (isActive) {
        const glowGeo = new THREE.BoxGeometry(1.1, 0.115, length)
        const glowMesh = new THREE.Mesh(glowGeo, isBottleneck && isCongested ? hazardLaserMat : activeLaserMat)
        glowMesh.position.copy(mid)
        glowMesh.position.y = 0.115
        glowMesh.lookAt(p2)
        activeRoadGroup.add(glowMesh)
      }

      // Congestion hazard beacon if congested
      if (isBottleneck && isCongested) {
        const hazardBoxGeo = new THREE.BoxGeometry(1.35, 0.14, length)
        const hazardBox = new THREE.Mesh(hazardBoxGeo, hazardLaserMat)
        hazardBox.position.copy(mid)
        hazardBox.position.y = 0.12
        hazardBox.lookAt(p2)
        hazardGroup.add(hazardBox)

        // Floating 3D holographic caution pin (Section 3.3)
        const sign = makeTextSprite('! CONGESTION HAZARD', '#ffffff', 'rgba(239, 68, 68, 0.95)', '#ef4444')
        sign.position.set(mid.x, 2.4, mid.z)
        sign.scale.set(3.8, 0.9, 1)
        hazardGroup.add(sign)

        // Flashing hazard point light
        const hazardLight = new THREE.PointLight(0xef4444, 3.0, 10)
        hazardLight.position.set(mid.x, 1.8, mid.z)
        hazardGroup.add(hazardLight)
      }
    })

    scene.add(roadGroup)
    scene.add(activeRoadGroup)
    scene.add(hazardGroup)

    // 6. Build 3D Nodes (Switches & Hosts)
    const nodeObjects: THREE.Object3D[] = []
    const switchGeo = new THREE.CylinderGeometry(0.95, 1.05, 0.95, 20)
    const switchMat = new THREE.MeshStandardMaterial({
      color: 0x1e252d,
      metalness: 0.8,
      roughness: 0.2,
    })
    const ringGeo = new THREE.TorusGeometry(1.05, 0.08, 12, 28)
    const ringMat = new THREE.MeshStandardMaterial({
      color: 0x38bdf8,
      emissive: 0x38bdf8,
      emissiveIntensity: 0.8,
    })

    const hostGeo = new THREE.BoxGeometry(1.1, 0.65, 1.1)
    const hostMat = new THREE.MeshStandardMaterial({
      color: 0x0f172a,
      metalness: 0.6,
      roughness: 0.3,
    })

    topologyDef.switches.forEach((sw) => {
      const p = pos3d.get(sw)
      if (!p) return

      const swGroup = new THREE.Group()
      swGroup.position.set(p.x, 0, p.z)
      swGroup.name = sw

      const pedestal = new THREE.Mesh(switchGeo, switchMat)
      pedestal.position.y = 0.48
      pedestal.castShadow = true
      swGroup.add(pedestal)

      const ring = new THREE.Mesh(ringGeo, ringMat)
      ring.position.y = 0.52
      ring.rotateX(Math.PI / 2)
      swGroup.add(ring)

      // Billboard label
      const isSelected = selected === sw
      const label = makeTextSprite(
        sw,
        isSelected ? '#38bdf8' : '#e2e8f0',
        isSelected ? 'rgba(14, 165, 233, 0.35)' : 'rgba(15, 23, 42, 0.90)',
        isSelected ? '#38bdf8' : '#475569'
      )
      label.position.y = 2.2
      swGroup.add(label)

      scene.add(swGroup)
      nodeObjects.push(swGroup)
    })

    topologyDef.hosts.forEach((h) => {
      const p = pos3d.get(h.name)
      if (!p) return

      const hostGroup = new THREE.Group()
      hostGroup.position.set(p.x, 0, p.z)
      hostGroup.name = h.name

      const terminal = new THREE.Mesh(hostGeo, hostMat)
      terminal.position.y = 0.33
      terminal.castShadow = true
      hostGroup.add(terminal)

      const beaconGeo = new THREE.SphereGeometry(0.16, 14, 14)
      const beaconMat = new THREE.MeshBasicMaterial({ color: 0x34d399 })
      const beacon = new THREE.Mesh(beaconGeo, beaconMat)
      beacon.position.y = 0.76
      hostGroup.add(beacon)

      const label = makeTextSprite(h.name, '#34d399', 'rgba(15, 23, 42, 0.90)', '#059669')
      label.position.y = 1.8
      label.scale.set(2.6, 0.8, 1)
      hostGroup.add(label)

      scene.add(hostGroup)
      nodeObjects.push(hostGroup)
    })

    // 7. 3D Highway Spline & Vehicles Animation
    const pathPoints: THREE.Vector3[] = []
    for (const nodeName of activePath) {
      const p = pos3d.get(nodeName)
      if (p) pathPoints.push(new THREE.Vector3(p.x, 0.12, p.z))
    }

    let curve: THREE.CatmullRomCurve3 | null = null
    if (pathPoints.length >= 2) {
      curve = new THREE.CatmullRomCurve3(pathPoints, false, 'catmullrom', 0.1)
    }

    const vehicleColors = ['#38bdf8', '#fbbf24', '#34d399', '#c084fc', '#f43f5e']
    const vehicleMeshes: THREE.Group[] = []
    const vehicleData = [
      { progress: 0.05, speed: 0.0022 },
      { progress: 0.26, speed: 0.0020 },
      { progress: 0.50, speed: 0.0025 },
      { progress: 0.72, speed: 0.0021 },
      { progress: 0.90, speed: 0.0024 },
    ]

    vehicleData.forEach((_, idx) => {
      const car = createVehicleMesh(vehicleColors[idx % vehicleColors.length])
      scene.add(car)
      vehicleMeshes.push(car)
    })

    // 8. Raycasting for Node Hover/Click
    const raycaster = new THREE.Raycaster()
    const mouse = new THREE.Vector2()

    const onPointerMove = (evt: MouseEvent) => {
      const rect = renderer.domElement.getBoundingClientRect()
      mouse.x = ((evt.clientX - rect.left) / rect.width) * 2 - 1
      mouse.y = -((evt.clientY - rect.top) / rect.height) * 2 + 1

      raycaster.setFromCamera(mouse, camera)
      const intersects = raycaster.intersectObjects(nodeObjects, true)
      if (intersects.length > 0) {
        let parent = intersects[0].object
        while (parent.parent && parent.parent !== scene) {
          parent = parent.parent
        }
        setHoveredNode(parent.name || null)
      } else {
        setHoveredNode(null)
      }
    }

    const onClick = (evt: MouseEvent) => {
      const rect = renderer.domElement.getBoundingClientRect()
      mouse.x = ((evt.clientX - rect.left) / rect.width) * 2 - 1
      mouse.y = -((evt.clientY - rect.top) / rect.height) * 2 + 1

      raycaster.setFromCamera(mouse, camera)
      const intersects = raycaster.intersectObjects(nodeObjects, true)
      if (intersects.length > 0) {
        let parent = intersects[0].object
        while (parent.parent && parent.parent !== scene) {
          parent = parent.parent
        }
        onSelect(parent.name || null)
      } else {
        onSelect(null)
      }
    }

    renderer.domElement.addEventListener('pointermove', onPointerMove)
    renderer.domElement.addEventListener('click', onClick)

    // 9. Main Render & Animation Loop
    let animId: number
    let lastTime = performance.now()
    const startTime = performance.now()

    const animate = () => {
      animId = requestAnimationFrame(animate)
      const now = performance.now()
      const delta = Math.min((now - lastTime) / 1000, 0.1)
      lastTime = now
      const elapsed = (now - startTime) / 1000

      // Auto-rotate if requested
      if (autoRotate && controlsRef.current) {
        controlsRef.current.autoRotate = true
        controlsRef.current.autoRotateSpeed = 1.0
      } else if (controlsRef.current) {
        controlsRef.current.autoRotate = false
      }
      controls.update()

      // Pulsing hazard glow animation
      if (isCongested) {
        const pulse = 0.6 + Math.sin(elapsed * 6) * 0.4
        hazardLaserMat.emissiveIntensity = pulse * 1.5
      }

      // Update vehicle positions along 3D curve
      vehicleMeshes.forEach((car) => {
        car.visible = !!curve
      })

      if (curve) {
        vehicleData.forEach((v, idx) => {
          v.progress += v.speed * (delta * 60)
          if (v.progress >= 1.0) v.progress -= 1.0

          const pt = curve!.getPointAt(v.progress)
          const tangent = curve!.getTangentAt(v.progress).normalize()

          const car = vehicleMeshes[idx]
          car.position.copy(pt)

          // Smooth orientation aligned with forward road tangent
          const target = pt.clone().add(tangent)
          car.lookAt(target)
        })

        // Camera follow mode on lead vehicle
        if (cameraPreset === 'follow' && vehicleMeshes[0]) {
          const leadCar = vehicleMeshes[0]
          const tangent = curve.getTangentAt(vehicleData[0].progress).normalize()
          const behind = leadCar.position.clone().sub(tangent.clone().multiplyScalar(5)).add(new THREE.Vector3(0, 3, 0))
          camera.position.lerp(behind, 0.08)
          controls.target.lerp(leadCar.position, 0.1)
        }
      }

      renderer.render(scene, camera)
    }

    animate()

    // Resize handler
    const onResize = () => {
      if (!container) return
      const w = container.clientWidth || 700
      const h = container.clientHeight || 450
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      renderer.setSize(w, h)
    }
    window.addEventListener('resize', onResize)

    return () => {
      cancelAnimationFrame(animId)
      window.removeEventListener('resize', onResize)
      renderer.domElement.removeEventListener('pointermove', onPointerMove)
      renderer.domElement.removeEventListener('click', onClick)
      controls.dispose()
      renderer.dispose()
      if (container.contains(renderer.domElement)) {
        container.removeChild(renderer.domElement)
      }
    }
  }, [topologyDef, activePath, bottleneckLink, isCongested, selected, onSelect, cameraPreset, autoRotate])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', minHeight: '450px' }}>
      <div ref={mountRef} style={{ width: '100%', height: '100%' }} />

      {/* 3D HUD Overlay */}
      <div
        style={{
          position: 'absolute',
          bottom: '10px',
          left: '12px',
          background: 'rgba(15, 23, 42, 0.85)',
          border: '1px solid #334155',
          borderRadius: '4px',
          padding: '5px 10px',
          fontSize: '11px',
          fontFamily: 'var(--mono)',
          color: '#cbd5e1',
          pointerEvents: 'none',
        }}
      >
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <span style={{ color: '#38bdf8' }}>🎮 3D View:</span>
          <span>Drag to Orbit | Scroll to Zoom | Right-click to Pan</span>
          {hoveredNode && (
            <span style={{ color: '#34d399', marginLeft: '10px' }}>
              • Node: <strong>{hoveredNode}</strong>
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
