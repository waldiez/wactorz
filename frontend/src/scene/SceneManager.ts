/**
 * Babylon.js scene lifecycle manager.
 *
 * Owns the Engine, Scene, ArcRotateCamera, and the active ThemeBase.
 * Also manages the {@link CardDashboard} overlay when the "cards" theme is active.
 *
 * Theme switching is handled by {@link setTheme}:
 *   graph / galaxy  →  Babylon.js 3D theme
 *   cards           →  HTML/CSS CardDashboard overlay (canvas still renders)
 */

import {
  Animation,
  ArcRotateCamera,
  Color4,
  CubicEase,
  EasingFunction,
  Engine,
  HemisphericLight,
  Scene,
  Vector3,
} from "@babylonjs/core";

import type {
  AgentInfo,
  HeartbeatPayload,
  AlertPayload,
  SpawnPayload,
} from "../types/agent";
import { ThemeBase } from "./themes/ThemeBase";
import { GraphTheme } from "./themes/GraphTheme";
import { CardDashboard } from "../ui/CardDashboard";
import { SocialDashboard } from "../ui/SocialDashboard";

export type ThemeName =
  | "cards"
  | "social";

// ── NullTheme — minimal placeholder used when HTML overlays are active ─────────

class NullTheme extends ThemeBase {
  readonly name: "cards" | "social";

  constructor(scene: Scene, variant: "cards" | "social" = "cards") {
    super(scene);
    this.name = variant;
  }

  setup(): void {
    this.scene.clearColor = new Color4(0.05, 0.08, 0.14, 1);
    // Gentle ambient so the canvas background looks intentional
    const light = new HemisphericLight(
      "null-ambient",
      new Vector3(0, 1, 0),
      this.scene,
    );
    light.intensity = 0.2;
  }

  teardown(): void {
    const l = this.scene.getLightByName("null-ambient");
    l?.dispose();
  }

  addAgent(): void {}
}

// ── SceneManager ──────────────────────────────────────────────────────────────

export class SceneManager {
  readonly engine: Engine;
  readonly scene: Scene;
  readonly camera: ArcRotateCamera;

  private agents: Map<string, AgentInfo> = new Map();
  private activeTheme: ThemeBase;
  private cardDashboard: CardDashboard | null = null;
  private socialDashboard: SocialDashboard | null = null;

  constructor(canvas: HTMLCanvasElement) {
    // ── Engine + Scene ────────────────────────────────────────────────────────
    this.engine = new Engine(canvas, true, {
      preserveDrawingBuffer: true,
      stencil: true,
      antialias: true,
    });

    this.scene = new Scene(this.engine);
    this.scene.clearColor.set(0.02, 0.03, 0.1, 1); // deep indigo

    // ── Camera ────────────────────────────────────────────────────────────────
    this.camera = new ArcRotateCamera(
      "camera",
      -Math.PI / 2,
      Math.PI / 3,
      20,
      Vector3.Zero(),
      this.scene,
    );
    this.camera.lowerRadiusLimit = 5;
    this.camera.upperRadiusLimit = 80;
    this.camera.inertia = 0.85;
    this.camera.wheelPrecision = 5;
    this.camera.attachControl(canvas, true);

    // ── Default theme ─────────────────────────────────────────────────────────
    // Use "cards" as the placeholder name so setTheme never needs to create a
    // dashboard here — the theme-change event from ThemeSwitcher will do it.
    this.activeTheme = new NullTheme(this.scene, "cards");

    // ── Render loop ───────────────────────────────────────────────────────────
    this.engine.runRenderLoop(() => this.scene.render());
    window.addEventListener("resize", () => this.engine.resize());
  }

  // ── Theme switching ─────────────────────────────────────────────────────────

  setTheme(name: ThemeName): void {
    // Skip if the right dashboard is already running.
    if (
      this.activeTheme.name === name &&
      !((name === "cards" && !this.cardDashboard) ||
        (name === "social" && !this.socialDashboard))
    ) return;

    this.activeTheme.teardown();

    // Tear down whichever HTML overlay is currently active
    if (this.cardDashboard) {
      this.cardDashboard.hide();
      this.cardDashboard = null;
    }
    if (this.socialDashboard) {
      this.socialDashboard.hide();
      this.socialDashboard = null;
    }

    if (name === "cards") {
      this.activeTheme = new NullTheme(this.scene, "cards");
      this.activeTheme.setup();
      this.cardDashboard = new CardDashboard();
      this.cardDashboard.show([...this.agents.values()]);
    } else if (name === "social") {
      this.activeTheme = new NullTheme(this.scene, "social");
      this.activeTheme.setup();
      this.socialDashboard = new SocialDashboard();
      this.socialDashboard.show([...this.agents.values()]);
    } else {
        this.activeTheme = new GraphTheme(this.scene);
        this.activeTheme.setup();
        for (const agent of this.agents.values()) {
          this.activeTheme.addAgent(agent);
        }
    }
  }

  get currentTheme(): ThemeName {
    return this.activeTheme.name as ThemeName;
  }

  /** Accept external theme-switch requests (e.g. from CardDashboard sub-theme toggle). */
  requestTheme(name: ThemeName): void {
    this.setTheme(name);
  }

  // ── Agent management ────────────────────────────────────────────────────────

  addOrUpdateAgent(agent: AgentInfo): void {
    // If another agent with the same NAME but a different ID already exists,
    // remove it first.  HLC-WID guarantees global uniqueness, so a second spawn
    // of the same logical agent produces a new ID — we treat that as a restart.
    for (const [oldId, oldAgent] of this.agents) {
      if (oldAgent.name === agent.name && oldId !== agent.id) {
        this.agents.delete(oldId);
        if (this.cardDashboard) this.cardDashboard.removeAgent(oldId);
        else if (this.socialDashboard) this.socialDashboard.removeAgent(oldId);
        else this.activeTheme.removeAgent(oldId);
        break;
      }
    }

    const existing = this.agents.get(agent.id);
    // Merge: keep existing metric fields if the incoming update doesn't include them.
    const merged: AgentInfo = existing ? { ...existing, ...agent } : agent;
    this.agents.set(agent.id, merged);
    if (this.cardDashboard) {
      existing
        ? this.cardDashboard.updateAgent(merged)
        : this.cardDashboard.addAgent(merged);
    } else if (this.socialDashboard) {
      existing
        ? this.socialDashboard.updateAgent(merged)
        : this.socialDashboard.addAgent(merged);
    } else {
      existing
        ? this.activeTheme.updateAgent(merged)
        : this.activeTheme.addAgent(merged);
    }
  }

  removeAgent(id: string): void {
    this.agents.delete(id);
    if (this.cardDashboard) this.cardDashboard.removeAgent(id);
    else if (this.socialDashboard) this.socialDashboard.removeAgent(id);
    else this.activeTheme.removeAgent(id);
  }

  onHeartbeat(payload: HeartbeatPayload): void {
    const agent = this.agents.get(payload.agentId);
    if (agent) {
      agent.state = payload.state;
      agent.lastHeartbeatAt = new Date(payload.timestampMs).toISOString();
      if (payload.cpu !== undefined) agent.cpu = payload.cpu;
      if (payload.memory_mb !== undefined) agent.mem = payload.memory_mb;
      if (payload.task !== undefined) agent.task = payload.task;
      if (this.cardDashboard)
        this.cardDashboard.onHeartbeat(payload.agentId, payload.timestampMs);
      else if (this.socialDashboard)
        this.socialDashboard.onHeartbeat(payload.agentId, payload.timestampMs);
      else this.activeTheme.onHeartbeat(payload.agentId);
    } else {
      this.addOrUpdateAgent({
        id: payload.agentId,
        name: payload.agentName,
        state: payload.state,
        protected: false,
        lastHeartbeatAt: new Date(
          Number.isFinite(payload.timestampMs)
            ? payload.timestampMs
            : Date.now(),
        ).toISOString(),
      });
    }
  }

  onAlert(payload: AlertPayload): void {
    if (this.cardDashboard)
      this.cardDashboard.showAlert(payload.agentId, payload.severity);
    else if (this.socialDashboard)
      this.socialDashboard.showAlert(payload.agentId, payload.severity);
    else this.activeTheme.onAlert(payload.agentId, payload.severity);
  }

  onChat(fromName: string, toName: string): void {
    let fromId: string | undefined;
    let toId: string | undefined;
    for (const agent of this.agents.values()) {
      if (agent.name === fromName) fromId = agent.id;
      if (agent.name === toName) toId = agent.id;
    }
    if (!fromId) return;
    if (this.cardDashboard) this.cardDashboard.onChat(fromId, toId ?? "");
    else if (this.socialDashboard)
      this.socialDashboard.onChat(fromId, toId ?? "");
    else if (toId) this.activeTheme.onChat(fromId, toId);
    else this.activeTheme.onHeartbeat(fromId);
  }

  onSpawn(payload: SpawnPayload): void {
    this.addOrUpdateAgent({
      id: payload.agentId,
      name: payload.agentName,
      state: "initializing",
      protected: false,
      agentType: payload.agentType,
    });
    if (!this.cardDashboard && !this.socialDashboard) {
      this.activeTheme.onSpawn(payload.agentId);
    }
  }

  /** Return all currently tracked agents (for mention-autocomplete etc.). */
  getAgents(): AgentInfo[] {
    return [...this.agents.values()];
  }

  /**
   * Smoothly pan the camera target to the given agent node.
   * No-op in cards mode (chat panel opens instead via agent-selected event).
   */
  onAgentSelected(agentId: string): void {
    if (this.cardDashboard || this.socialDashboard) return; // HTML overlay
    const node = this.activeTheme.getNode(agentId);
    if (!node) return;

    const ease = new CubicEase();
    ease.setEasingMode(EasingFunction.EASINGMODE_EASEINOUT);
    Animation.CreateAndStartAnimation(
      "camPan",
      this.camera,
      "target",
      30,
      30,
      this.camera.target.clone(),
      node.position.clone(),
      Animation.ANIMATIONLOOPMODE_CONSTANT,
      ease,
    );
  }

  // ── Cleanup ─────────────────────────────────────────────────────────────────

  dispose(): void {
    this.cardDashboard?.destroy();
    this.socialDashboard?.destroy();
    this.activeTheme.teardown();
    this.engine.dispose();
  }
}
