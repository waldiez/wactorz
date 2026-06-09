// Babylon digital twin built dynamically from the geometry document.
// EnergyPlus is Z-up; Babylon is Y-up → map (x,y,z)_EP → (x, z, y)_Babylon,
// recentred on the building footprint.
import {
  ArcRotateCamera, Color3, Color4, Constants, Engine, GlowLayer, HemisphericLight,
  Mesh, MeshBuilder, Scene, StandardMaterial, Vector3, VertexData,
} from "@babylonjs/core";
import type { Geometry } from "./data";
import { tempColor } from "./data";

export class Twin {
  private engine: Engine;
  private scene: Scene;
  private camera: ArcRotateCamera;
  private zoneMesh = new Map<string, Mesh>();
  private zoneMat = new Map<string, StandardMaterial>();
  private floorMeshes = new Map<string, Mesh[]>();
  private windowMeshes: Mesh[] = [];
  private target = new Map<string, [number, number, number]>(); // smoothed colour target

  constructor(canvas: HTMLCanvasElement, geo: Geometry) {
    this.engine = new Engine(canvas, true, { antialias: true, adaptToDeviceRatio: true });
    this.scene = new Scene(this.engine);
    this.scene.clearColor = new Color4(0.024, 0.039, 0.078, 1);

    // The office is wide and low (50×33 m footprint, ~2.7 m floors), so a true-scale
    // model reads as flat plates. Exaggerate the vertical axis so it reads as a building.
    const VSCALE = 2.6;
    const { min, max } = geo.bbox;
    const cx = (min[0] + max[0]) / 2, cy = (min[1] + max[1]) / 2;
    const span = Math.max(max[0] - min[0], max[1] - min[1]);
    const topY = max[2] * VSCALE;
    const ep = (v: number[]) => new Vector3(v[0] - cx, v[2] * VSCALE, v[1] - cy);

    this.camera = new ArcRotateCamera("cam", Math.PI * 0.85, Math.PI * 0.36,
      span * 1.7, new Vector3(0, topY * 0.45, 0), this.scene);
    this.camera.attachControl(canvas, true);
    this.camera.wheelPrecision = 8;
    this.camera.lowerRadiusLimit = span * 0.6;
    this.camera.upperRadiusLimit = span * 3.5;
    this.camera.upperBetaLimit = Math.PI * 0.495;

    const hemi = new HemisphericLight("h", new Vector3(0.3, 1, 0.2), this.scene);
    hemi.intensity = 0.95;
    hemi.groundColor = new Color3(0.05, 0.08, 0.15);

    const glow = new GlowLayer("glow", this.scene);
    glow.intensity = 0.6;

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
          const p = ep(v); positions.push(p.x, p.y, p.z);
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

      const mat = new StandardMaterial(name + "_m", this.scene);
      mat.specularColor = new Color3(0.05, 0.05, 0.08);
      if (z.occupied) {
        mat.alpha = 0.82;
        mat.emissiveColor = new Color3(0.12, 0.16, 0.26);
      } else {
        mat.alpha = 0.10;                 // plenums: faint slabs
        mat.diffuseColor = new Color3(0.4, 0.46, 0.6);
        mat.emissiveColor = new Color3(0.04, 0.05, 0.08);
      }
      mesh.material = mat;
      mesh.enableEdgesRendering();
      mesh.edgesWidth = z.occupied ? 2.0 : 1.0;
      mesh.edgesColor = new Color4(0.5, 0.62, 0.9, z.occupied ? 0.5 : 0.2);

      this.zoneMesh.set(name, mesh);
      this.zoneMat.set(name, mat);
      const arr = this.floorMeshes.get(z.floor) ?? [];
      arr.push(mesh); this.floorMeshes.set(z.floor, arr);
      if (z.occupied) this.target.set(name, [0.13, 0.18, 0.3]);
    }

    // ── glazing: windows (glass) + doors, from the fenestration geometry ────────
    // Glass uses ADDITIVE blending so it only adds a cyan glow to the façade and
    // never darkens the zones behind it (combine-mode transparent panes stacked over
    // the building were muting the tints). This also makes the toggle obvious.
    // Warm "lit window" glazing: additive amber (never darkens the building) that pops
    // against the cool temp-tinted floors, with crisp warm frames per pane.
    const glass = new StandardMaterial("glass", this.scene);
    glass.diffuseColor = new Color3(0, 0, 0);
    glass.emissiveColor = new Color3(0.85, 0.55, 0.20);
    glass.alpha = 0.62; glass.alphaMode = Constants.ALPHA_ADD;
    glass.backFaceCulling = false; glass.disableLighting = true;
    const doorMat = new StandardMaterial("door", this.scene);
    doorMat.diffuseColor = new Color3(0.16, 0.20, 0.28);
    doorMat.emissiveColor = new Color3(0.05, 0.07, 0.11);
    doorMat.alpha = 0.55; doorMat.backFaceCulling = false;

    for (const w of geo.windows ?? []) {
      if (!w.vertices || w.vertices.length < 3) continue;
      const positions: number[] = [];
      const indices: number[] = [];
      for (const v of w.vertices) {
        const p = ep(v);
        // sit just proud of the wall so panes read clearly without z-fighting
        const len = Math.hypot(p.x, p.z) || 1;
        positions.push(p.x + (p.x / len) * 0.25, p.y, p.z + (p.z / len) * 0.25);
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
      mesh.edgesColor = isDoor ? new Color4(0.5, 0.6, 0.8, 0.3) : new Color4(1.0, 0.82, 0.48, 0.9);
      this.windowMeshes.push(mesh);   // toggled together via setWindowsVisible
    }

    // smooth colour lerp each frame
    this.scene.onBeforeRenderObservable.add(() => {
      for (const [name, mat] of this.zoneMat) {
        const t = this.target.get(name); if (!t) continue;
        const c = mat.diffuseColor;
        c.r += (t[0] - c.r) * 0.08;
        c.g += (t[1] - c.g) * 0.08;
        c.b += (t[2] - c.b) * 0.08;
        mat.emissiveColor.set(c.r * 0.6, c.g * 0.6, c.b * 0.6);
      }
    });

    this.engine.runRenderLoop(() => this.scene.render());
    addEventListener("resize", () => this.engine.resize());
  }

  setZoneTemp(zone: string, temp: number) {
    if (!this.zoneMat.has(zone)) return;
    this.target.set(zone, tempColor(temp));
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

  setFloorVisible(floor: string, visible: boolean) {
    for (const m of this.floorMeshes.get(floor) ?? []) m.setEnabled(visible);
  }

  setWindowsVisible(visible: boolean) {
    for (const m of this.windowMeshes) m.setEnabled(visible);
  }
}
