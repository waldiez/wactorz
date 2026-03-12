/**
 * Abstract base for all agent 3D nodes.
 *
 * Handles:
 * - Click selection via pointerObservable
 * - Label (Babylon.js GUI TextBlock above the mesh)
 * - Generic pulse / alert / spawn animation stubs
 */

import {
  AbstractMesh,
  ActionManager,
  DynamicTexture,
  ExecuteCodeAction,
  Mesh,
  MeshBuilder,
  StandardMaterial,
  Color3,
  type Scene,
} from "@babylonjs/core";

import type { AgentInfo, AgentState } from "../../types/agent";

export abstract class AgentNodeBase {
  /** The primary 3D mesh representing this agent. */
  abstract readonly mesh: AbstractMesh;

  /** Whether this node is pinned at the scene origin (main actor). */
  readonly isMainActor: boolean;

  /** Callback fired when the user clicks this node. */
  onClick: ((agent: AgentInfo) => void) | null = null;

  protected info: AgentInfo;
  private labelMesh: Mesh | null = null;

  constructor(
    info: AgentInfo,
    protected readonly scene: Scene,
    isMainActor: boolean,
  ) {
    this.info = info;
    this.isMainActor = isMainActor;
  }

  /**
   * Create a floating name label above the mesh.
   * Call from subclass constructors after `this.mesh` is assigned.
   *
   * @param yOffset  Distance above the mesh centre (default 0.8).
   */
  protected createLabel(yOffset = 0.8): void {
    const texW = 512, texH = 96;
    const texture = new DynamicTexture(`label-tex-${this.info.id}`, { width: texW, height: texH }, this.scene, true);
    texture.hasAlpha = true;

    const ctx = texture.getContext() as CanvasRenderingContext2D;
    ctx.clearRect(0, 0, texW, texH);
    ctx.font = "bold 30px monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    // subtle dark halo for readability
    ctx.shadowColor = "rgba(0,0,0,0.8)";
    ctx.shadowBlur = 6;
    ctx.fillStyle = this.isMainActor ? "rgba(255, 210, 100, 0.95)" : "rgba(180, 210, 255, 0.9)";
    ctx.fillText(this.info.name, texW / 2, texH / 2);
    texture.update();

    const plane = MeshBuilder.CreatePlane(`label-${this.info.id}`, { width: 1.8, height: 0.34 }, this.scene);
    plane.parent = this.mesh;
    plane.position.y = yOffset;
    plane.billboardMode = Mesh.BILLBOARDMODE_ALL;
    plane.isPickable = false;

    const mat = new StandardMaterial(`label-mat-${this.info.id}`, this.scene);
    mat.diffuseTexture = texture;
    mat.emissiveColor = Color3.White();
    mat.useAlphaFromDiffuseTexture = true;
    mat.disableLighting = true;
    mat.backFaceCulling = false;
    plane.material = mat;

    this.labelMesh = plane;
  }

  /** Dispose the label mesh (call from subclass dispose()). */
  protected disposeLabel(): void {
    if (this.labelMesh) {
      this.labelMesh.material?.dispose(false, true);
      this.labelMesh.dispose();
      this.labelMesh = null;
    }
  }

  /** Register click + hover handlers on the mesh after it has been created. */
  protected registerClick(): void {
    if (!this.mesh.actionManager) {
      this.mesh.actionManager = new ActionManager(this.scene);
    }
    this.mesh.actionManager.registerAction(
      new ExecuteCodeAction(ActionManager.OnPickTrigger, () => {
        this.onClick?.(this.info);
      }),
    );

    // Hover tooltip
    this.mesh.actionManager.registerAction(
      new ExecuteCodeAction(ActionManager.OnPointerOverTrigger, () => {
        AgentNodeBase.showTooltip(this.info);
      }),
    );
    this.mesh.actionManager.registerAction(
      new ExecuteCodeAction(ActionManager.OnPointerOutTrigger, () => {
        AgentNodeBase.hideTooltip();
      }),
    );
  }

  private static showTooltip(info: AgentInfo): void {
    const el = document.getElementById("node-tooltip");
    if (!el) return;
    const state = typeof info.state === "object" ? "failed" : (info.state ?? "unknown");
    const hb = info.lastHeartbeatAt
      ? new Date(info.lastHeartbeatAt).toLocaleTimeString()
      : "—";
    el.innerHTML = `
      <div class="tt-name">${info.name}</div>
      <div class="tt-row"><span class="tt-key">state</span><span class="tt-val">${state}</span></div>
      <div class="tt-row"><span class="tt-key">heartbeat</span><span class="tt-val">${hb}</span></div>
    `;
    el.style.display = "block";
  }

  private static hideTooltip(): void {
    const el = document.getElementById("node-tooltip");
    if (el) el.style.display = "none";
  }

  /** Update the agent state (e.g. change emissive colour). */
  onStateChange(state: AgentState): void {
    this.info = { ...this.info, state };
    this.applyStateVisuals(state);
  }

  /** Override to react to state changes visually. */
  protected abstract applyStateVisuals(state: AgentState): void;

  /** Animate a gentle glow pulse (heartbeat). */
  abstract pulseHeartbeat(): void;

  /** Animate an alert shockwave. */
  abstract showAlert(severity: string): void;

  /** Animate a spawn materialise effect. */
  abstract playSpawnEffect(): void;

  /** Clean up all Babylon.js resources. */
  abstract dispose(): void;

  // ── Convenience getters ───────────────────────────────────────────────────

  get id(): string { return this.info.id; }
  get agentName(): string { return this.info.name; }

  get position() {
    return this.mesh.position;
  }
}
