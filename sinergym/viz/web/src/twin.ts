// Babylon digital twin built dynamically from the geometry document.
// EnergyPlus is Z-up; Babylon is Y-up → map (x,y,z)_EP → (x, z, y)_Babylon,
// recentred on the building footprint.
import {
  ArcRotateCamera, Camera, Color3, Color4, Constants, Engine, GlowLayer, HemisphericLight,
  Matrix, Mesh, MeshBuilder, Scene, StandardMaterial, Vector3, VertexData,
} from "@babylonjs/core";
import type { Geometry, ZoneGeo } from "./data";
import { tempColor } from "./data";

export class Twin {
  private engine: Engine;
  private scene: Scene;
  private camera: ArcRotateCamera;
  private hemi!: HemisphericLight;
  private sky = new Color4(0.024, 0.039, 0.078, 1);   // lerp target for the backdrop
  private wet = 0;                                     // 1 while it is raining
  private zoneMesh = new Map<string, Mesh>();
  private zoneMat = new Map<string, StandardMaterial>();
  private floorMeshes = new Map<string, Mesh[]>();
  private windowMeshes: Mesh[] = [];
  private target = new Map<string, [number, number, number]>(); // smoothed colour target
  // occupancy "presence" orbs: a warm light per occupied zone that grows with people
  private presence = new Map<string, { mesh: Mesh; mat: StandardMaterial; cur: number; tgt: number }>();
  private zoneCenter = new Map<string, Vector3>();   // exploded centroid, for camera focus
  private highlight = new Set<string>();             // zones lifted by hover
  private focused: string | null = null;             // zone the camera is locked onto
  private camTarget!: Vector3;                        // eased camera target
  private homeTarget!: Vector3;                       // default (whole-building) target
  private span = 1;                                   // footprint span, for ortho bounds
  private planMode = false;                           // top-down floor-plan lens
  private orbit = { alpha: 0, beta: 0, radius: 0 };   // saved 3D pose, restored on exit
  private labelBox?: HTMLDivElement;                  // overlay holding the per-zone labels
  private labels = new Map<string, HTMLDivElement>(); // zone → its plan-view label

  constructor(canvas: HTMLCanvasElement, geo: Geometry) {
    this.engine = new Engine(canvas, true, { antialias: true, adaptToDeviceRatio: true });
    this.scene = new Scene(this.engine);
    this.scene.clearColor = new Color4(0.024, 0.039, 0.078, 1);

    // The office is wide and low (50×33 m footprint, ~2.7 m floors), so a true-scale
    // model reads as flat plates. Exaggerate the vertical axis so it reads as a building.
    const VSCALE = 4.6;
    const { min, max } = geo.bbox;
    const cx = (min[0] + max[0]) / 2, cy = (min[1] + max[1]) / 2;
    const span = Math.max(max[0] - min[0], max[1] - min[1]);
    this.span = span;
    const ep = (v: number[]) => new Vector3(v[0] - cx, v[2] * VSCALE, v[1] - cy);

    // Zones tile the footprint edge-to-edge, so the thin perimeter strips blur into
    // the huge core and read as one mass. Shrink each zone horizontally toward its own
    // footprint centroid so a clear gap opens between neighbours — true proportions kept,
    // just pulled apart enough to tell the 5 zones/floor apart (the "better representation").
    const INSET = 0.8;
    const insetXY = (v: number[], c: [number, number, number]): number[] =>
      [c[0] + (v[0] - c[0]) * INSET, c[1] + (v[1] - c[1]) * INSET, v[2]];

    // Floor explode: pull each storey apart vertically so the floors read as distinct,
    // stacked slabs. floorIdx maps a z-height to its storey; the gap scales with floor height.
    const occBases = [...new Set(Object.values(geo.zones)
      .filter((z) => z.occupied).map((z) => Math.round(z.z_range[0] * 100) / 100))].sort((a, b) => a - b);
    const floorH = occBases.length > 1 ? (occBases[1] - occBases[0]) * VSCALE : 8;
    const GAP = floorH * 0.13;
    const floorIdx = (z: number) => { let i = 0; for (let k = 0; k < occBases.length; k++) if (occBases[k] <= z + 0.05) i = k; return i; };
    const explode = (z: number) => floorIdx(z) * GAP;
    const nFloors = Math.max(1, occBases.length);
    const topY = max[2] * VSCALE + (nFloors - 1) * GAP;

    this.camera = new ArcRotateCamera("cam", Math.PI * 0.86, Math.PI * 0.37,
      span * 1.95, new Vector3(0, topY * 0.42, 0), this.scene);
    this.camTarget = this.camera.target.clone();
    this.homeTarget = this.camera.target.clone();
    // gentle idle orbit (read-only showcase); auto-pauses on interaction, resumes when idle
    this.camera.useAutoRotationBehavior = true;
    const arb = this.camera.autoRotationBehavior!;
    arb.idleRotationSpeed = 0.12;
    arb.idleRotationWaitTime = 2500;
    arb.idleRotationSpinupTime = 1600;
    arb.zoomStopsAnimation = true;
    this.camera.attachControl(canvas, true);
    this.camera.wheelPrecision = 8;
    this.camera.lowerRadiusLimit = span * 0.6;
    this.camera.upperRadiusLimit = span * 3.5;
    this.camera.upperBetaLimit = Math.PI * 0.495;

    const hemi = new HemisphericLight("h", new Vector3(0.3, 1, 0.2), this.scene);
    hemi.intensity = 0.95;
    hemi.groundColor = new Color3(0.05, 0.08, 0.15);
    this.hemi = hemi;

    // Calmer glow so temp colours stay crisp and the glazing never blooms to white.
    const glow = new GlowLayer("glow", this.scene);
    glow.intensity = 0.32;

    // ground grid
    const ground = MeshBuilder.CreateGround("g", { width: span * 2.4, height: span * 2.4 }, this.scene);
    const gm = new StandardMaterial("gm", this.scene);
    gm.diffuseColor = new Color3(0.03, 0.05, 0.1);
    gm.specularColor = new Color3(0, 0, 0);
    gm.alpha = 0.65;
    ground.material = gm;
    ground.position.y = -0.05;

    // build a mesh per zone
    for (const [name, z] of Object.entries(geo.zones)) {
      const positions: number[] = [];
      const indices: number[] = [];
      for (const surf of z.surfaces) {
        const base = positions.length / 3;
        for (const v of surf.vertices) {
          const p = ep(insetXY(v, z.centroid)); positions.push(p.x, p.y, p.z);
        }
        for (let i = 1; i < surf.vertices.length - 1; i++) {
          indices.push(base, base + i, base + i + 1);
        }
      }
      if (!positions.length) continue;
      const mesh = new Mesh(name, this.scene);
      const vd = new VertexData();
      vd.positions = positions; vd.indices = indices;
      const normals: number[] = [];
      VertexData.ComputeNormals(positions, indices, normals);
      vd.normals = normals;
      vd.applyToMesh(mesh);
      mesh.flipFaces(true); // surfaces wind toward zone interior
      mesh.position.y += explode(z.z_range[0]);   // lift into its exploded storey

      const mat = new StandardMaterial(name + "_m", this.scene);
      mat.specularColor = new Color3(0.05, 0.05, 0.08);
      if (z.occupied) {
        mat.alpha = 0.9;                  // solid, readable volumes (like the old view)
        mat.emissiveColor = new Color3(0.10, 0.14, 0.24);
      } else {
        mat.alpha = 0.08;                 // plenums: faint slabs
        mat.diffuseColor = new Color3(0.4, 0.46, 0.6);
        mat.emissiveColor = new Color3(0.04, 0.05, 0.08);
      }
      mesh.material = mat;
      mesh.enableEdgesRendering();
      mesh.edgesWidth = z.occupied ? 2.4 : 1.0;
      mesh.edgesColor = new Color4(0.62, 0.74, 1.0, z.occupied ? 0.7 : 0.18);

      this.zoneMesh.set(name, mesh);
      this.zoneMat.set(name, mat);
      const arr = this.floorMeshes.get(z.floor) ?? [];
      arr.push(mesh); this.floorMeshes.set(z.floor, arr);
      if (z.occupied) {
        this.target.set(name, [0.13, 0.18, 0.3]);
        // a warm presence light at the zone centroid — dark until people arrive
        const orb = MeshBuilder.CreateSphere(name + "_occ", { diameter: 1.4, segments: 12 }, this.scene);
        const c = ep(z.centroid); c.y += explode(z.z_range[0]);
        orb.position.set(c.x, c.y, c.z);
        this.zoneCenter.set(name, c.clone());
        // Render the presence orb in a later group so it's never occluded by the zone
        // shell it sits inside — occupancy stays readable on every floor, not just the top.
        orb.renderingGroupId = 1;
        const om = new StandardMaterial(name + "_occm", this.scene);
        om.diffuseColor = new Color3(0, 0, 0);
        om.emissiveColor = new Color3(0.0, 0.0, 0.0);
        om.disableLighting = true;
        om.disableDepthWrite = true;
        om.alpha = 0.9; om.alphaMode = Constants.ALPHA_ADD;
        orb.material = om;
        orb.scaling.setAll(0.01);
        this.presence.set(name, { mesh: orb, mat: om, cur: 0, tgt: 0 });
      }
    }

    // ── floor decks: thin dark slabs at each storey base + the roof, so the
    // building reads as stacked floors (the architectural look of the prior view) ──
    const fw = (max[0] - min[0]) * 1.05, fd = (max[1] - min[1]) * 1.05;
    const occ = Object.values(geo.zones).filter((z) => z.occupied);
    const deckZ = [...new Set(occ.map((z) => Math.round(z.z_range[0] * 100) / 100))].sort((a, b) => a - b);
    if (occ.length) deckZ.push(Math.max(...occ.map((z) => z.z_range[1])));   // roof
    for (const z of deckZ) {
      const deck = MeshBuilder.CreateGround("deck_" + z, { width: fw, height: fd }, this.scene);
      deck.position.y = z * VSCALE - 0.02 + explode(z);
      const dm = new StandardMaterial("deckm_" + z, this.scene);
      dm.diffuseColor = new Color3(0.05, 0.07, 0.12);
      dm.specularColor = new Color3(0.02, 0.03, 0.05);
      dm.emissiveColor = new Color3(0.02, 0.03, 0.05);
      dm.alpha = 0.94;
      deck.material = dm;
      deck.enableEdgesRendering();
      deck.edgesWidth = 1.4;
      deck.edgesColor = new Color4(0.4, 0.5, 0.78, 0.35);
    }

    // ── glazing: windows (glass) + doors, from the fenestration geometry ────────
    // Glass uses ADDITIVE blending so it only adds a cyan glow to the façade and
    // never darkens the zones behind it (combine-mode transparent panes stacked over
    // the building were muting the tints). This also makes the toggle obvious.
    // Warm "lit window" glazing: additive amber (never darkens the building) that pops
    // against the cool temp-tinted floors, with crisp warm frames per pane.
    const glass = new StandardMaterial("glass", this.scene);
    glass.diffuseColor = new Color3(0, 0, 0);
    glass.emissiveColor = new Color3(0.42, 0.27, 0.11);   // subtle warm glaze, not a bloom
    glass.alpha = 0.32; glass.alphaMode = Constants.ALPHA_ADD;
    glass.backFaceCulling = false; glass.disableLighting = true;
    const doorMat = new StandardMaterial("door", this.scene);
    doorMat.diffuseColor = new Color3(0.16, 0.20, 0.28);
    doorMat.emissiveColor = new Color3(0.05, 0.07, 0.11);
    doorMat.alpha = 0.55; doorMat.backFaceCulling = false;

    for (const w of geo.windows ?? []) {
      if (!w.vertices || w.vertices.length < 3) continue;
      const positions: number[] = [];
      const indices: number[] = [];
      const wy = explode(Math.min(...w.vertices.map((v) => v[2])));   // window's storey lift
      const wc = w.zone ? geo.zones[w.zone]?.centroid : null;          // track the inset wall
      for (const v of w.vertices) {
        const p = ep(wc ? insetXY(v, wc) : v);
        // sit just proud of the wall so panes read clearly without z-fighting
        const len = Math.hypot(p.x, p.z) || 1;
        positions.push(p.x + (p.x / len) * 0.25, p.y + wy, p.z + (p.z / len) * 0.25);
      }
      for (let i = 1; i < w.vertices.length - 1; i++) indices.push(0, i, i + 1);
      const mesh = new Mesh("fen_" + w.surface, this.scene);
      const vd = new VertexData();
      vd.positions = positions; vd.indices = indices;
      const normals: number[] = [];
      VertexData.ComputeNormals(positions, indices, normals);
      vd.normals = normals; vd.applyToMesh(mesh);
      const isDoor = w.kind === "Door";
      mesh.material = isDoor ? doorMat : glass;
      mesh.enableEdgesRendering();
      mesh.edgesWidth = isDoor ? 1.4 : 2.0;
      mesh.edgesColor = isDoor ? new Color4(0.5, 0.6, 0.8, 0.3) : new Color4(0.9, 0.68, 0.36, 0.55);
      this.windowMeshes.push(mesh);   // toggled together via setWindowsVisible
    }

    // ── plan-view labels: one HTML chip per occupied zone, parked over its centroid.
    // Hidden in 3D; shown (and reprojected each frame) only in the top-down plan lens.
    const host = canvas.parentElement;
    if (host) {
      const box = document.createElement("div");
      box.className = "zone-labels";
      host.appendChild(box);
      this.labelBox = box;
      // Compass from the DOE OfficeMedium perimeter naming (ZN_1=N, _2=E, _3=S, _4=W) —
      // authoritative for this model; geometry position alone can't recover true facing
      // without the building's north axis. Falls back to no compass for other naming.
      const COMPASS: Record<string, string> = { "1": "north", "2": "east", "3": "south", "4": "west" };
      // Floor-polygon area (shoelace on x,y) → conditioned m² for the zone.
      const area = (z: ZoneGeo): number => {
        const f = z.surfaces.find((s) => /floor/i.test(s.type)) ??
          z.surfaces.find((s) => /ceil|roof/i.test(s.type));
        if (!f) return 0;
        let a = 0;
        for (let i = 0; i < f.vertices.length; i++) {
          const [x1, y1] = f.vertices[i], [x2, y2] = f.vertices[(i + 1) % f.vertices.length];
          a += x1 * y2 - x2 * y1;
        }
        return Math.abs(a) / 2;
      };
      for (const name of this.zoneCenter.keys()) {
        const z = geo.zones[name];
        const znum = name.match(/ZN_?(\d+)/i)?.[1];
        const title = /core/i.test(name) ? "Core"
          : znum ? `ZN ${znum}${COMPASS[znum] ? " " + COMPASS[znum] : ""}` : name;
        const m2 = z ? Math.round(area(z)) : 0;
        const el = document.createElement("div");
        el.className = "zone-label";
        el.innerHTML = `<b>${title}</b>${m2 ? `<span>${m2} m²</span>` : ""}`;
        box.appendChild(el);
        this.labels.set(name, el);
      }
    }

    // smooth colour lerp + occupancy presence pulse each frame
    let clk = 0;
    this.scene.onBeforeRenderObservable.add(() => {
      for (const [name, mat] of this.zoneMat) {
        const t = this.target.get(name); if (!t) continue;
        const c = mat.diffuseColor;
        c.r += (t[0] - c.r) * 0.08;
        c.g += (t[1] - c.g) * 0.08;
        c.b += (t[2] - c.b) * 0.08;
        const k = this.highlight.has(name) ? 1.15 : 0.6;   // hovered zone glows brighter
        mat.emissiveColor.set(c.r * k, c.g * k, c.b * k);
      }
      // ease the backdrop toward the time-of-day target
      const cc = this.scene.clearColor;
      cc.r += (this.sky.r - cc.r) * 0.04;
      cc.g += (this.sky.g - cc.g) * 0.04;
      cc.b += (this.sky.b - cc.b) * 0.04;
      // ease the camera toward its focus target (zone focus / reset)
      this.camera.target.addInPlace(this.camTarget.subtract(this.camera.target).scale(0.08));
      clk += 0.05;
      const breathe = 0.82 + 0.18 * Math.sin(clk);   // gentle "alive" pulse
      for (const p of this.presence.values()) {
        p.cur += (p.tgt - p.cur) * 0.06;
        const lit = p.cur * breathe;
        // warm amber → white-hot as a zone fills up
        p.mat.emissiveColor.set(0.95 * lit, 0.66 * lit + 0.18 * lit * p.cur, 0.30 * lit);
        const s = 0.4 + 1.7 * p.cur;
        p.mesh.scaling.setAll(Math.max(0.01, s));
        p.mesh.setEnabled(p.cur > 0.02);
      }
      if (this.planMode) this.updateLabels();
    });

    this.engine.runRenderLoop(() => this.scene.render());
    addEventListener("resize", () => {
      this.engine.resize();
      if (this.planMode) this.applyOrtho();
    });
  }

  setZoneTemp(zone: string, temp: number) {
    this.setZoneColor(zone, tempColor(temp));
  }

  /** Generic per-zone colour target (used by temp / occupancy / setpoint view modes). */
  setZoneColor(zone: string, rgb: [number, number, number]) {
    if (!this.zoneMat.has(zone)) return;
    this.target.set(zone, rgb);
  }

  /** Drive the backdrop + light from the simulated hour: night → dawn → day → dusk. */
  setTimeOfDay(hour: number) {
    const h = (((hour % 24) + 24) % 24);
    const lerp = (stops: [number, number[]][], v: number): number[] => {
      if (v <= stops[0][0]) return stops[0][1];
      for (let i = 0; i < stops.length - 1; i++) {
        const [a, ca] = stops[i], [b, cb] = stops[i + 1];
        if (v >= a && v <= b) {
          const f = (v - a) / (b - a || 1);
          return ca.map((c, k) => c + (cb[k] - c) * f);
        }
      }
      return stops[stops.length - 1][1];
    };
    const sky = lerp([
      [0, [0.02, 0.03, 0.06]], [6, [0.06, 0.07, 0.15]], [9, [0.05, 0.10, 0.19]],
      [13, [0.07, 0.12, 0.21]], [17, [0.13, 0.10, 0.17]], [19, [0.17, 0.08, 0.10]],
      [21, [0.05, 0.04, 0.09]], [24, [0.02, 0.03, 0.06]],
    ], h);
    let [sr, sg, sb] = sky;
    let [it] = lerp([
      [0, [0.5]], [7, [0.8]], [12, [1.15]], [17, [0.98]], [19, [0.72]], [22, [0.5]], [24, [0.5]],
    ], h);
    // warm the daylight in the afternoon, cool it overnight
    let tint = lerp([
      [0, [0.55, 0.65, 1.0]], [9, [0.95, 0.97, 1.0]], [13, [1.0, 1.0, 0.98]],
      [18, [1.0, 0.86, 0.66]], [21, [0.6, 0.66, 1.0]], [24, [0.55, 0.65, 1.0]],
    ], h);
    // rain: flatten toward a cool overcast — greyer sky, dimmer, blue-tinted light
    const w = this.wet;
    if (w > 0) {
      const mix = (a: number, b: number) => a + (b - a) * w;
      sr = mix(sr, 0.09); sg = mix(sg, 0.11); sb = mix(sb, 0.15);
      it = mix(it, it * 0.72);
      tint = [mix(tint[0], 0.62), mix(tint[1], 0.72), mix(tint[2], 0.95)];
    }
    this.sky.set(sr, sg, sb, 1);
    this.hemi.intensity = it;
    this.hemi.diffuse.set(tint[0], tint[1], tint[2]);
  }

  /** Toggle the rainy-weather mood (paired with the CSS rain overlay on the stage). */
  setRaining(on: boolean) { this.wet = on ? 1 : 0; }

  /** People count for a zone → presence-orb intensity (saturates ~8 people). */
  setZoneOccupancy(zone: string, people: number) {
    const p = this.presence.get(zone);
    if (!p) return;
    p.tgt = Number.isFinite(people) ? Math.min(1, Math.max(0, people / 8)) : 0;
  }

  /** Hover affordance: brighten a zone's glow + edges. */
  highlightZone(zone: string, on: boolean) {
    if (on) this.highlight.add(zone); else this.highlight.delete(zone);
    const m = this.zoneMesh.get(zone);
    if (!m) return;
    m.edgesWidth = on ? 4.5 : 2.4;
    m.edgesColor = on ? new Color4(1, 1, 1, 0.95) : new Color4(0.62, 0.74, 1.0, 0.7);
  }

  /** Click affordance: glide the camera to centre a zone; click again to reset. Returns
   *  true while a zone is focused, false once it resets to the whole-building view. */
  focusZone(zone: string): boolean {
    const c = this.zoneCenter.get(zone);
    if (!c) return false;
    if (this.focused === zone) { this.focused = null; this.camTarget = this.homeTarget.clone(); return false; }
    this.focused = zone;
    this.camTarget = c.clone();
    return true;
  }

  flashZone(zone: string) {
    const mat = this.zoneMat.get(zone); if (!mat) return;
    let t = 0;
    const id = setInterval(() => {
      t += 0.12;
      const p = Math.abs(Math.sin(t * Math.PI));
      mat.emissiveColor.set(0.9 * p + 0.1, 0.18, 0.28);
      if (t >= 2) { clearInterval(id); }
    }, 40);
  }

  /** On-screen zoom: scale the camera radius, clamped to its limits (mult<1 = zoom in). */
  zoom(mult: number) {
    const lo = this.camera.lowerRadiusLimit ?? 0;
    const hi = this.camera.upperRadiusLimit ?? Infinity;
    this.camera.radius = Math.min(hi, Math.max(lo, this.camera.radius * mult));
  }

  /** Toggle the top-down floor-plan lens: same meshes + live colours, orthographic from
   *  straight above, with zone labels. The 3D orbit pose is saved and restored on exit. */
  setPlanView(on: boolean) {
    if (on === this.planMode) return;
    this.planMode = on;
    const cam = this.camera;
    if (on) {
      this.orbit = { alpha: cam.alpha, beta: cam.beta, radius: cam.radius };
      cam.useAutoRotationBehavior = false;
      cam.mode = Camera.ORTHOGRAPHIC_CAMERA;
      cam.alpha = -Math.PI / 2;   // north (─y_EP) points up
      cam.beta = 0.001;           // look straight down
      cam.target = this.homeTarget.clone();
      this.applyOrtho();
      this.labelBox?.classList.add("on");
    } else {
      cam.mode = Camera.PERSPECTIVE_CAMERA;
      cam.alpha = this.orbit.alpha; cam.beta = this.orbit.beta; cam.radius = this.orbit.radius;
      cam.useAutoRotationBehavior = true;
      this.labelBox?.classList.remove("on");
    }
  }

  /** Size the orthographic frustum to the footprint and viewport aspect. */
  private applyOrtho() {
    const h = this.span * 0.62;
    const aspect = this.engine.getRenderWidth() / Math.max(1, this.engine.getRenderHeight());
    this.camera.orthoTop = h; this.camera.orthoBottom = -h;
    this.camera.orthoLeft = -h * aspect; this.camera.orthoRight = h * aspect;
  }

  /** Project each zone centroid to screen and park its label there (plan view only). */
  private updateLabels() {
    const canvas = this.engine.getRenderingCanvas();
    if (!canvas) return;
    const rw = this.engine.getRenderWidth(), rh = this.engine.getRenderHeight();
    const vp = this.camera.viewport.toGlobal(rw, rh);
    // Project returns render-buffer pixels; the CSS overlay is in CSS pixels. Convert with
    // the real canvas client size (robust to any devicePixelRatio / display scaling).
    const sx = canvas.clientWidth / rw, sy = canvas.clientHeight / rh;
    for (const [name, el] of this.labels) {
      const center = this.zoneCenter.get(name);
      const mesh = this.zoneMesh.get(name);
      if (!center || !mesh?.isEnabled()) { el.style.display = "none"; continue; }
      const p = Vector3.Project(center, Matrix.Identity(), this.scene.getTransformMatrix(), vp);
      el.style.display = "block";
      el.style.left = `${p.x * sx}px`;
      el.style.top = `${p.y * sy}px`;
    }
  }

  setFloorVisible(floor: string, visible: boolean) {
    for (const m of this.floorMeshes.get(floor) ?? []) m.setEnabled(visible);
  }

  setWindowsVisible(visible: boolean) {
    for (const m of this.windowMeshes) m.setEnabled(visible);
  }
}
