/**
 * Galaxy-theme agent node: sphere with atmospheric glow + elliptical orbit.
 *
 * Main actor = central star (large emissive sphere, no orbit).
 * Other agents = planets that rotate around the origin on their orbit ring.
 */

import {
  Color3,
  Color4,
  HighlightLayer,
  Mesh,
  MeshBuilder,
  StandardMaterial,
  Vector3,
  type Scene,
} from "@babylonjs/core";

import type { AgentInfo, AgentState } from "../../types/agent";
import { AgentNodeBase } from "./AgentNodeBase";

/** Planet colour palette (cycles through for each new agent). */
const PLANET_COLORS: Color3[] = [
  new Color3(0.3, 0.7, 1.0),  // ice blue
  new Color3(0.7, 0.4, 1.0),  // violet
  new Color3(0.3, 1.0, 0.6),  // teal
  new Color3(1.0, 0.5, 0.3),  // orange
  new Color3(0.8, 0.8, 0.3),  // yellow
];

let colorIndex = 0;

export class PlanetNode extends AgentNodeBase {
  readonly mesh: Mesh;
  private material: StandardMaterial;
  private orbitRing: Mesh | null = null;
  private orbitAngle = Math.random() * Math.PI * 2;
  private readonly orbitSpeed: number;
  private readonly orbitRadius: number;
  private highlightLayer: HighlightLayer | null;
  private orbitObserver: ReturnType<Scene["onBeforeRenderObservable"]["add"]> | null = null;

  constructor(
    info: AgentInfo,
    scene: Scene,
    orbitRadius: number,
    isMainActor: boolean,
    highlightLayer: HighlightLayer | null,
  ) {
    super(info, scene, isMainActor);
    this.orbitRadius = orbitRadius;
    this.orbitSpeed = 0.002 + Math.random() * 0.003;
    this.highlightLayer = highlightLayer;

    const radius = isMainActor ? 1.2 : 0.4 + Math.random() * 0.3;
    this.mesh = MeshBuilder.CreateSphere(
      `planet-${info.id}`,
      { diameter: radius * 2, segments: 20 },
      scene,
    );

    // ── Material ──────────────────────────────────────────────────────────────
    this.material = new StandardMaterial(`planet-mat-${info.id}`, scene);
    if (isMainActor) {
      this.material.emissiveColor = new Color3(1.0, 0.85, 0.3);
      this.material.diffuseColor = new Color3(1.0, 0.7, 0.1);
    } else {
      const c = PLANET_COLORS[colorIndex % PLANET_COLORS.length] ?? PLANET_COLORS[0]!;
      colorIndex++;
      this.material.diffuseColor = c;
      this.material.specularColor = c.scale(0.4);
      this.material.emissiveColor = c.scale(0.15);
    }
    this.mesh.position = isMainActor
      ? Vector3.Zero()
      : new Vector3(orbitRadius, 0, 0);
    this.mesh.material = this.material;

    // ── Atmosphere highlight ──────────────────────────────────────────────────
    if (highlightLayer && !isMainActor) {
      const atmColor = this.material.diffuseColor.scale(0.6);
      highlightLayer.addMesh(this.mesh, atmColor);
    }

    // ── Orbit ring ────────────────────────────────────────────────────────────
    if (!isMainActor && orbitRadius > 0) {
      this.orbitRing = MeshBuilder.CreateTorus(
        `orbit-${info.id}`,
        { diameter: orbitRadius * 2, thickness: 0.02, tessellation: 64 },
        scene,
      );
      const ringMat = new StandardMaterial(`orbit-mat-${info.id}`, scene);
      ringMat.emissiveColor = new Color3(0.3, 0.4, 0.7);
      ringMat.alpha = 0.25;
      this.orbitRing.material = ringMat;
      this.orbitRing.rotation.x = Math.PI / 2;
    }

    // ── Orbital animation ─────────────────────────────────────────────────────
    if (!isMainActor) {
      this.orbitObserver = scene.onBeforeRenderObservable.add(() => {
        this.orbitAngle += this.orbitSpeed;
        this.mesh.position.x = Math.cos(this.orbitAngle) * this.orbitRadius;
        this.mesh.position.z = Math.sin(this.orbitAngle) * this.orbitRadius;
        this.mesh.rotation.y += 0.005; // self-rotation
      });
    }

    this.registerClick();
    this.createLabel(radius + 0.5);
  }

  protected applyStateVisuals(state: AgentState): void {
    if (state === "stopped" || state === "paused") {
      this.material.emissiveColor = new Color3(0.2, 0.2, 0.3);
    } else if (typeof state === "object" && "failed" in state) {
      this.material.emissiveColor = new Color3(0.8, 0.1, 0.1);
    } else {
      this.material.emissiveColor = this.isMainActor
        ? new Color3(1.0, 0.85, 0.3)
        : this.material.diffuseColor.scale(0.15);
    }
  }

  pulseHeartbeat(): void {
    let t = 0;
    const orig = this.mesh.scaling.clone();
    const obs = this.scene.onBeforeRenderObservable.add(() => {
      t = Math.min(t + 0.07, 1);
      const s = 1 + 0.18 * Math.sin(t * Math.PI);
      this.mesh.scaling.setAll(s);
      if (t >= 1) {
        this.mesh.scaling = orig;
        this.scene.onBeforeRenderObservable.remove(obs);
      }
    });
  }

  showAlert(severity: string): void {
    const flash = severity === "critical"
      ? new Color3(1, 0.05, 0.05)
      : new Color3(1, 0.7, 0.1);
    const prev = this.material.emissiveColor.clone();
    this.material.emissiveColor = flash;
    setTimeout(() => { this.material.emissiveColor = prev; }, 700);
  }

  playSpawnEffect(): void {
    this.mesh.scaling.setAll(0);
    let t = 0;
    const obs = this.scene.onBeforeRenderObservable.add(() => {
      t = Math.min(t + 0.04, 1);
      this.mesh.scaling.setAll(t);
      if (t >= 1) {
        this.mesh.scaling.setAll(1);
        this.scene.onBeforeRenderObservable.remove(obs);
      }
    });
  }

  dispose(): void {
    this.disposeLabel();
    if (this.orbitObserver) {
      this.scene.onBeforeRenderObservable.remove(this.orbitObserver);
    }
    if (this.highlightLayer) {
      this.highlightLayer.removeMesh(this.mesh);
    }
    this.orbitRing?.material?.dispose();
    this.orbitRing?.dispose();
    this.material.dispose();
    this.mesh.dispose();
  }
}
