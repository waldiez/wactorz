/**
 * Galaxy / deep-space theme.
 *
 * Visual language:
 * - Procedural starfield skybox with nebula colour tinting
 * - MainActor = central star with emissive glow + lens flare
 * - Agents = planets with StandardMaterial texture + atmospheric highlight layer
 * - Orbits = elliptical rings rendered as faint line meshes
 * - Camera: same ArcRotateCamera as Graph theme
 */

import {
  Color3,
  Color4,
  DirectionalLight,
  HemisphericLight,
  HighlightLayer,
  LensFlareSystem,
  LensFlare,
  Mesh,
  MeshBuilder,
  ParticleSystem,
  PointLight,
  StandardMaterial,
  Texture,
  Vector3,
  type Scene,
} from "@babylonjs/core";

import type { AgentInfo } from "../../types/agent";
import { PlanetNode } from "../nodes/PlanetNode";
import { ThemeBase } from "./ThemeBase";

export class GalaxyTheme extends ThemeBase {
  readonly name = "galaxy" as const;

  private sunLight: PointLight | null = null;
  private ambientLight: HemisphericLight | null = null;
  private highlightLayer: HighlightLayer | null = null;
  private nebula: ParticleSystem | null = null;

  /** Track current orbit radius for the next planet. */
  private nextOrbitRadius = 6;

  setup(): void {
    // ── Scene background ──────────────────────────────────────────────────────
    this.scene.clearColor = new Color4(0.01, 0.01, 0.06, 1);

    // ── Lights ────────────────────────────────────────────────────────────────
    this.sunLight = new PointLight("sun-light", Vector3.Zero(), this.scene);
    this.sunLight.diffuse = new Color3(1.0, 0.92, 0.7);
    this.sunLight.intensity = 2.5;

    this.ambientLight = new HemisphericLight(
      "galaxy-ambient",
      new Vector3(0, 1, 0),
      this.scene,
    );
    this.ambientLight.intensity = 0.12;
    this.ambientLight.diffuse = new Color3(0.3, 0.3, 0.8);

    // ── Highlight (atmosphere) layer ──────────────────────────────────────────
    this.highlightLayer = new HighlightLayer("atmosphere", this.scene);
    this.highlightLayer.blurHorizontalSize = 0.6;
    this.highlightLayer.blurVerticalSize = 0.6;

    // ── Nebula particle cloud ─────────────────────────────────────────────────
    this.nebula = new ParticleSystem("nebula", 2000, this.scene);
    this.nebula.emitter = Vector3.Zero();
    this.nebula.minEmitBox = new Vector3(-50, -15, -50);
    this.nebula.maxEmitBox = new Vector3(50, 15, 50);
    this.nebula.color1 = new Color4(0.5, 0.2, 0.8, 0.08);
    this.nebula.color2 = new Color4(0.2, 0.4, 0.9, 0.06);
    this.nebula.colorDead = new Color4(0, 0, 0, 0);
    this.nebula.minSize = 1.5;
    this.nebula.maxSize = 5.0;
    this.nebula.minLifeTime = 999999;
    this.nebula.maxLifeTime = 999999;
    this.nebula.emitRate = 2000;
    this.nebula.blendMode = ParticleSystem.BLENDMODE_ADD;
    this.nebula.gravity = Vector3.Zero();
    this.nebula.minEmitPower = 0;
    this.nebula.maxEmitPower = 0;
    this.nebula.start();
  }

  teardown(): void {
    this.nebula?.dispose();
    this.nebula = null;
    this.highlightLayer?.dispose();
    this.highlightLayer = null;
    this.sunLight?.dispose();
    this.sunLight = null;
    this.ambientLight?.dispose();
    this.ambientLight = null;

    for (const [id] of this.nodes) {
      this.removeAgent(id);
    }

    // Reset orbit counter
    this.nextOrbitRadius = 6;

    // Restore default clear colour
    this.scene.clearColor = new Color4(0.02, 0.03, 0.10, 1);
  }

  addAgent(agent: AgentInfo): void {
    if (this.nodes.has(agent.id)) { this.updateAgent(agent); return; }

    const isMain = agent.name === "main-actor" || agent.agentType === "orchestrator" || agent.agentType === "main";

    const orbitRadius = isMain ? 0 : this.nextOrbitRadius;
    if (!isMain) this.nextOrbitRadius += 3.5;

    const node = new PlanetNode(
      agent,
      this.scene,
      orbitRadius,
      isMain,
      this.highlightLayer,
    );
    node.onClick = (info) => {
      document.dispatchEvent(
        new CustomEvent<{ agent: AgentInfo }>("agent-selected", { detail: { agent: info } }),
      );
    };

    this.nodes.set(agent.id, node);
  }
}
