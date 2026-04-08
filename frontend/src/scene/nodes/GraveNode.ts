/**
 * Π-απαθάνατοι ("Papadeath") theme node.
 *
 * Each agent is a hovering zombie entity: a teetering tombstone box with a
 * sickly neon-green glow, animated with a slow sine-wave bob.
 *
 * Main actor = the Boss — a large skull-sphere with pulsing crimson eyes.
 */

import {
  Color3,
  Color4,
  GlowLayer,
  Mesh,
  MeshBuilder,
  PointLight,
  StandardMaterial,
  Vector3,
  type Scene,
  type AbstractMesh,
} from "@babylonjs/core";

import type { AgentInfo, AgentState } from "../../types/agent";
import { AgentNodeBase } from "./AgentNodeBase";

/** Velocity vector for the undead layout shamble. */
export interface Velocity3 {
  x: number;
  y: number;
  z: number;
}

// Graveyard palette
const DECAY_GREEN = new Color3(0.18, 0.78, 0.25);
const BONE_WHITE = new Color3(0.85, 0.88, 0.72);
const ZOMBIE_PURPLE = new Color3(0.55, 0.2, 0.75);
const TOXIC = new Color3(0.38, 0.85, 0.1);
const BLOOD_RED = new Color3(0.9, 0.05, 0.05);

const PALETTE = [DECAY_GREEN, TOXIC, ZOMBIE_PURPLE, BONE_WHITE];

let graveIndex = 0;

export class GraveNode extends AgentNodeBase {
  readonly mesh: Mesh;
  private mat: StandardMaterial;
  private eyeLight: PointLight | null = null;
  private bobObserver: ReturnType<
    Scene["onBeforeRenderObservable"]["add"]
  > | null = null;
  private bobPhase: number;
  private baseY: number = 0;

  /** Layout velocity for the shamble (spring layout). */
  velocity: Velocity3 = { x: 0, y: 0, z: 0 };

  constructor(
    info: AgentInfo,
    scene: Scene,
    position: Vector3,
    isMainWactor: boolean,
  ) {
    super(info, scene, isMainWactor);
    this.bobPhase = Math.random() * Math.PI * 2;

    // ── Mesh ─────────────────────────────────────────────────────────────────
    if (isMainWactor) {
      // Boss: a large sphere (skull)
      this.mesh = MeshBuilder.CreateSphere(
        `grave-${info.id}`,
        { diameter: 1.5, segments: 14 },
        scene,
      );
    } else {
      // Others: a tall thin box (tombstone)
      this.mesh = MeshBuilder.CreateBox(
        `grave-${info.id}`,
        { width: 0.55, height: 1.1, depth: 0.12 },
        scene,
      );
    }
    this.mesh.position = position.clone();
    this.baseY = position.y;

    // ── Material ─────────────────────────────────────────────────────────────
    this.mat = new StandardMaterial(`grave-mat-${info.id}`, scene);
    if (isMainWactor) {
      this.mat.emissiveColor = BLOOD_RED;
      this.mat.diffuseColor = new Color3(0.3, 0.02, 0.02);
    } else {
      const col = PALETTE[graveIndex % PALETTE.length] ?? DECAY_GREEN;
      graveIndex++;
      this.mat.emissiveColor = col.scale(0.4);
      this.mat.diffuseColor = col.scale(0.15);
    }
    this.mat.disableLighting = false;
    this.mesh.material = this.mat;

    // ── Eye/halo light ────────────────────────────────────────────────────────
    this.eyeLight = new PointLight(
      `grave-eye-${info.id}`,
      position.clone(),
      scene,
    );
    this.eyeLight.diffuse = isMainWactor ? BLOOD_RED : DECAY_GREEN;
    this.eyeLight.intensity = isMainWactor ? 1.8 : 0.5;
    this.eyeLight.range = isMainWactor ? 10 : 4;

    // Keep light at mesh position
    scene.onBeforeRenderObservable.add(() => {
      if (this.eyeLight) this.eyeLight.position = this.mesh.position.clone();
    });

    // ── Glow ─────────────────────────────────────────────────────────────────
    let glow = scene.getGlowLayerByName("grave-glow");
    if (!glow) {
      glow = new GlowLayer("grave-glow", scene);
      glow.intensity = 0.9;
    }
    glow.addIncludedOnlyMesh(this.mesh);

    // ── Bob animation ─────────────────────────────────────────────────────────
    this.bobObserver = scene.onBeforeRenderObservable.add(() => {
      this.bobPhase += 0.018;
      this.mesh.position.y = this.baseY + 0.15 * Math.sin(this.bobPhase);
      // Tombstones also sway slightly on the Z axis
      if (!isMainWactor) {
        this.mesh.rotation.z = 0.05 * Math.sin(this.bobPhase * 0.7 + 1.2);
      }
    });

    this.registerClick();
    this.createLabel(isMainWactor ? 0.95 : 0.75);
  }

  /** Called by GraveTheme's layout to update horizontal position. */
  setBasePosition(x: number, z: number): void {
    this.mesh.position.x = x;
    this.mesh.position.z = z;
    this.baseY = 0; // keep everything at ground level
  }

  protected applyStateVisuals(state: AgentState): void {
    if (state === "stopped") {
      this.mat.emissiveColor = new Color3(0.15, 0.15, 0.2);
      if (this.eyeLight) this.eyeLight.intensity = 0.05;
    } else if (typeof state === "object" && "failed" in state) {
      this.mat.emissiveColor = BLOOD_RED;
      if (this.eyeLight) {
        this.eyeLight.diffuse = BLOOD_RED;
        this.eyeLight.intensity = 1.5;
      }
    } else {
      const col = this.isMainWactor ? BLOOD_RED : DECAY_GREEN;
      this.mat.emissiveColor = col.scale(0.4);
      if (this.eyeLight) {
        this.eyeLight.diffuse = col;
        this.eyeLight.intensity = this.isMainWactor ? 1.8 : 0.5;
      }
    }
  }

  pulseHeartbeat(): void {
    let t = 0;
    const obs = this.scene.onBeforeRenderObservable.add(() => {
      t = Math.min(t + 0.07, 1);
      const s = 1 + 0.22 * Math.sin(t * Math.PI);
      this.mesh.scaling.setAll(s);
      if (t >= 1) {
        this.mesh.scaling.setAll(1);
        this.scene.onBeforeRenderObservable.remove(obs);
      }
    });
  }

  showAlert(severity: string): void {
    const flash =
      severity === "critical" || severity === "error" ? BLOOD_RED : TOXIC;
    const prev = this.mat.emissiveColor.clone();
    this.mat.emissiveColor = flash;
    setTimeout(() => {
      this.mat.emissiveColor = prev;
    }, 600);
  }

  playSpawnEffect(): void {
    // Rise from the grave
    const startY = this.baseY - 2;
    this.mesh.position.y = startY;
    this.mesh.scaling.setAll(0.01);
    let t = 0;
    const obs = this.scene.onBeforeRenderObservable.add(() => {
      t = Math.min(t + 0.04, 1);
      this.mesh.scaling.setAll(t);
      this.mesh.position.y = startY + (this.baseY - startY) * t;
      if (t >= 1) {
        this.mesh.scaling.setAll(1);
        this.scene.onBeforeRenderObservable.remove(obs);
      }
    });
  }

  dispose(): void {
    this.disposeLabel();
    if (this.bobObserver) {
      this.scene.onBeforeRenderObservable.remove(this.bobObserver);
    }
    const glow = this.scene.getGlowLayerByName("grave-glow");
    if (glow) glow.removeIncludedOnlyMesh(this.mesh);
    this.eyeLight?.dispose();
    this.mat.dispose();
    this.mesh.dispose();
  }
}
