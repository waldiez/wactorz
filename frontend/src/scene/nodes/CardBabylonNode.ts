/**
 * Cards-3D theme agent node.
 *
 * A flat vertical plane with a DynamicTexture card face showing:
 *   avatar letter · name · type · state badge · truncated ID.
 *
 * Waldiez palette matches the HTML CardDashboard.
 */

import {
  Color3,
  DynamicTexture,
  Mesh,
  MeshBuilder,
  StandardMaterial,
  type Scene,
  type AbstractMesh,
} from "@babylonjs/core";

import type { AgentInfo, AgentState } from "../../types/agent";
import { AgentNodeBase } from "./AgentNodeBase";

const CARD_W = 2.4;
const CARD_H = 3.2;
const TEX_W  = 288;
const TEX_H  = 384;

function accentHex(info: AgentInfo): string {
  if (info.name === "main-actor" || info.agentType === "orchestrator") return "#f59e0b";
  if (typeof info.state === "object") return "#f43f5e";
  switch (info.state as string) {
    case "running":      return "#3dd68c";
    case "paused":       return "#fb923c";
    case "initializing": return "#60a5fa";
    case "stopped":      return "#475569";
    default:             return "#3dd68c";
  }
}

function stateLabel(state: AgentState): string {
  if (typeof state === "object") return "FAILED";
  return (state as string).toUpperCase();
}

export class CardBabylonNode extends AgentNodeBase {
  readonly mesh: Mesh;
  private mat: StandardMaterial;
  private tex: DynamicTexture;

  constructor(info: AgentInfo, scene: Scene, isMainActor: boolean) {
    super(info, scene, isMainActor);

    this.tex = new DynamicTexture(
      `card3d-tex-${info.id}`,
      { width: TEX_W, height: TEX_H },
      scene,
      false,
    );
    this.tex.hasAlpha = true;

    this.mat = new StandardMaterial(`card3d-mat-${info.id}`, scene);
    this.mat.diffuseTexture  = this.tex;
    this.mat.emissiveTexture = this.tex;
    this.mat.emissiveColor   = Color3.White();
    this.mat.disableLighting = true;
    this.mat.backFaceCulling = false;

    this.mesh = MeshBuilder.CreatePlane(
      `card3d-${info.id}`,
      { width: CARD_W, height: CARD_H, sideOrientation: Mesh.DOUBLESIDE },
      scene,
    );
    this.mesh.material = this.mat;

    this.drawCard();
    this.registerClick();
  }

  // position getter is inherited from AgentNodeBase (returns this.mesh.position)

  private drawCard(): void {
    const ctx = this.tex.getContext() as CanvasRenderingContext2D;
    ctx.clearRect(0, 0, TEX_W, TEX_H);

    const accent = accentHex(this.info);
    const isMain = this.isMainActor;
    const stStr  = stateLabel(this.info.state);

    // ── Background ───────────────────────────────────────────────────────────
    ctx.fillStyle = isMain ? "#1c1407" : "#0d1424";
    ctx.beginPath();
    ctx.roundRect(3, 3, TEX_W - 6, TEX_H - 6, 14);
    ctx.fill();

    // ── Top accent bar ────────────────────────────────────────────────────────
    ctx.fillStyle = accent;
    ctx.beginPath();
    ctx.roundRect(3, 3, TEX_W - 6, 8, [14, 14, 0, 0]);
    ctx.fill();

    // ── Border ────────────────────────────────────────────────────────────────
    ctx.strokeStyle = accent + "55";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.roundRect(3, 3, TEX_W - 6, TEX_H - 6, 14);
    ctx.stroke();

    // ── Avatar circle ─────────────────────────────────────────────────────────
    const cx = TEX_W / 2;
    const cy = 90;
    const r  = 34;
    ctx.globalAlpha = 0.2;
    ctx.fillStyle = accent;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = accent;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.stroke();

    // avatar letter
    ctx.fillStyle = accent;
    ctx.font = "bold 30px 'Segoe UI', Arial, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(this.info.name.charAt(0).toUpperCase(), cx, cy);

    // ── Name ─────────────────────────────────────────────────────────────────
    ctx.fillStyle = "#e2e8f0";
    ctx.font = "bold 17px 'Segoe UI', Arial, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const nameText = this.info.name.length > 18
      ? this.info.name.slice(0, 16) + "…"
      : this.info.name;
    ctx.fillText(nameText, cx, 140);

    // ── Agent type ────────────────────────────────────────────────────────────
    if (this.info.agentType) {
      ctx.fillStyle = "#64748b";
      ctx.font = "12px 'Segoe UI', Arial, sans-serif";
      ctx.fillText(this.info.agentType, cx, 163);
    }

    // ── Divider ───────────────────────────────────────────────────────────────
    ctx.strokeStyle = "#1e3050";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(28, 192);
    ctx.lineTo(TEX_W - 28, 192);
    ctx.stroke();

    // ── State badge ───────────────────────────────────────────────────────────
    const badgeW = 100;
    const badgeH = 26;
    const badgeX = (TEX_W - badgeW) / 2;
    const badgeY = 208;
    ctx.fillStyle = accent + "30";
    ctx.beginPath();
    ctx.roundRect(badgeX, badgeY, badgeW, badgeH, 8);
    ctx.fill();
    ctx.strokeStyle = accent + "aa";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.roundRect(badgeX, badgeY, badgeW, badgeH, 8);
    ctx.stroke();
    ctx.fillStyle = accent;
    ctx.font = "bold 11px 'Segoe UI', Arial, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(stStr, cx, badgeY + badgeH / 2);

    // ── ID ────────────────────────────────────────────────────────────────────
    ctx.fillStyle = "#334155";
    ctx.font = "10px monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(`…${this.info.id.slice(-18)}`, cx, 252);

    this.tex.update();
  }

  protected applyStateVisuals(_state: AgentState): void {
    this.drawCard(); // re-render with new state colour
  }

  pulseHeartbeat(): void {
    let t = 0;
    const obs = this.scene.onBeforeRenderObservable.add(() => {
      t = Math.min(t + 0.09, 1);
      const s = 1 + 0.06 * Math.sin(t * Math.PI);
      this.mesh.scaling.setAll(s);
      if (t >= 1) {
        this.mesh.scaling.setAll(1);
        this.scene.onBeforeRenderObservable.remove(obs);
      }
    });
  }

  showAlert(severity: string): void {
    const flash = severity === "error" || severity === "critical"
      ? new Color3(1, 0.15, 0.15)
      : new Color3(1, 0.75, 0.1);
    const prev = this.mat.emissiveColor.clone();
    this.mat.emissiveTexture = null;
    this.mat.emissiveColor = flash;
    setTimeout(() => {
      this.mat.emissiveColor = prev;
      this.mat.emissiveTexture = this.tex;
    }, 700);
  }

  playSpawnEffect(): void {
    this.mesh.scaling.setAll(0);
    let t = 0;
    const obs = this.scene.onBeforeRenderObservable.add(() => {
      t = Math.min(t + 0.05, 1);
      this.mesh.scaling.setAll(t);
      if (t >= 1) {
        this.mesh.scaling.setAll(1);
        this.scene.onBeforeRenderObservable.remove(obs);
      }
    });
  }

  dispose(): void {
    this.tex.dispose();
    this.mat.dispose();
    this.mesh.dispose();
  }
}
