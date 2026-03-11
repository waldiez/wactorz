/**
 * Graph-theme agent node: glowing sphere with point light halo.
 *
 * Main actor: larger amber/gold sphere, pinned at origin.
 * Other agents: smaller cyan/electric-blue spheres, positions managed by
 * the force-directed layout in {@link GraphTheme}.
 */

import {
  Color3,
  Color4,
  GlowLayer,
  Mesh,
  MeshBuilder,
  PBRMaterial,
  PointLight,
  StandardMaterial,
  Vector3,
  type Scene,
  type AbstractMesh,
} from "@babylonjs/core";

import type { AgentInfo, AgentState } from "../../types/agent";
import { AgentNodeBase } from "./AgentNodeBase";

/** Velocity vector used by the spring layout. */
export interface Velocity3 {
  x: number; y: number; z: number;
}

export class GraphNode extends AgentNodeBase {
  readonly mesh: Mesh;
  private material: StandardMaterial;
  private halo: PointLight;
  private glowLayer: GlowLayer | null = null;

  /** Layout velocity (mutated each frame by GraphTheme). */
  velocity: Velocity3 = { x: 0, y: 0, z: 0 };

  constructor(
    info: AgentInfo,
    scene: Scene,
    position: Vector3,
    isMainActor: boolean,
  ) {
    super(info, scene, isMainActor);

    const radius = isMainActor ? 0.7 : 0.35;
    this.mesh = MeshBuilder.CreateSphere(
      `node-${info.id}`,
      { diameter: radius * 2, segments: 16 },
      scene,
    );
    this.mesh.position = position.clone();

    // ── Material ──────────────────────────────────────────────────────────────
    this.material = new StandardMaterial(`mat-${info.id}`, scene);
    this.material.emissiveColor = isMainActor
      ? new Color3(1.0, 0.65, 0.1) // amber/gold
      : new Color3(0.1, 0.7, 1.0); // cyan
    this.material.disableLighting = true;
    this.mesh.material = this.material;

    // ── Point light halo ──────────────────────────────────────────────────────
    this.halo = new PointLight(`halo-${info.id}`, position.clone(), scene);
    this.halo.diffuse = isMainActor
      ? new Color3(1.0, 0.7, 0.3)
      : new Color3(0.2, 0.6, 1.0);
    this.halo.intensity = isMainActor ? 1.5 : 0.6;
    this.halo.range = isMainActor ? 8 : 4;

    // Keep halo tracking mesh
    scene.onBeforeRenderObservable.add(() => {
      this.halo.position = this.mesh.position.clone();
    });

    // ── Glow layer (shared per-scene) ─────────────────────────────────────────
    let glow = scene.getGlowLayerByName("graph-glow");
    if (!glow) {
      glow = new GlowLayer("graph-glow", scene);
      glow.intensity = 0.7;
    }
    this.glowLayer = glow;
    this.glowLayer.addIncludedOnlyMesh(this.mesh);

    this.registerClick();
    this.createLabel(radius + 0.45);
  }

  protected applyStateVisuals(state: AgentState): void {
    if (state === "stopped" || state === "paused") {
      this.material.emissiveColor = new Color3(0.3, 0.3, 0.5);
      this.halo.intensity = 0.1;
    } else if (typeof state === "object" && "failed" in state) {
      this.material.emissiveColor = new Color3(1.0, 0.2, 0.2);
      this.halo.diffuse = new Color3(1, 0.1, 0.1);
    } else {
      this.material.emissiveColor = this.isMainActor
        ? new Color3(1.0, 0.65, 0.1)
        : new Color3(0.1, 0.7, 1.0);
      this.halo.intensity = this.isMainActor ? 1.5 : 0.6;
    }
  }

  pulseHeartbeat(): void {
    // Scale briefly up then back
    const original = this.mesh.scaling.clone();
    const target = original.scale(1.25);
    let t = 0;
    const obs = this.scene.onBeforeRenderObservable.add(() => {
      t += 0.08;
      const s = 1 + 0.25 * Math.sin(t * Math.PI);
      this.mesh.scaling.setAll(s);
      if (t >= 1) {
        this.mesh.scaling = original;
        this.scene.onBeforeRenderObservable.remove(obs);
      }
    });
  }

  showAlert(severity: string): void {
    const flash = severity === "critical" || severity === "error"
      ? new Color3(1, 0.1, 0.1)
      : new Color3(1, 0.8, 0.1);
    const prev = this.material.emissiveColor.clone();
    this.material.emissiveColor = flash;
    setTimeout(() => { this.material.emissiveColor = prev; }, 600);
  }

  playSpawnEffect(): void {
    this.mesh.scaling.setAll(0);
    let t = 0;
    const obs = this.scene.onBeforeRenderObservable.add(() => {
      t = Math.min(t + 0.06, 1);
      this.mesh.scaling.setAll(t < 0.5 ? t * 2 : 1 + 0.1 * Math.sin((t - 0.5) * Math.PI * 4));
      if (t >= 1) {
        this.mesh.scaling.setAll(1);
        this.scene.onBeforeRenderObservable.remove(obs);
      }
    });
  }

  dispose(): void {
    this.disposeLabel();
    this.glowLayer?.removeIncludedOnlyMesh(this.mesh);
    this.halo.dispose();
    this.material.dispose();
    this.mesh.dispose();
  }
}
