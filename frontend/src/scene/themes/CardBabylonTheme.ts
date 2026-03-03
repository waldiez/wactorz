/**
 * Cards-3D theme — Babylon.js grid card layout.
 *
 * Arranges agents as flat plane cards in a responsive grid.
 * Good camera position: slightly elevated front view along -Z.
 *
 * Acts as the "3D" sub-mode of the Cards view; the HTML overlay
 * (CardDashboard) is NOT shown in this mode.
 */

import {
  Color3,
  Color4,
  HemisphericLight,
  Vector3,
  type Scene,
} from "@babylonjs/core";

import type { AgentInfo } from "../../types/agent";
import { CardBabylonNode } from "../nodes/CardBabylonNode";
import { ThemeBase } from "./ThemeBase";

const COLS      = 4;   // cards per row
const GAP_X     = 3.0; // horizontal spacing (world units)
const GAP_Y     = 4.0; // vertical spacing
const CARD_DEPTH = 0;  // all cards sit at z = 0

export class CardBabylonTheme extends ThemeBase {
  readonly name = "cards-3d" as const;

  private ambientLight: HemisphericLight | null = null;

  setup(): void {
    this.scene.clearColor = new Color4(0.06, 0.09, 0.16, 1);

    this.ambientLight = new HemisphericLight(
      "cards3d-ambient",
      new Vector3(0, 1, 0),
      this.scene,
    );
    this.ambientLight.intensity = 1.0;
    this.ambientLight.diffuse   = Color3.White();
  }

  teardown(): void {
    this.ambientLight?.dispose();
    this.ambientLight = null;

    for (const [id] of this.nodes) {
      this.removeAgent(id);
    }

    this.scene.clearColor = new Color4(0.02, 0.03, 0.10, 1);
  }

  addAgent(agent: AgentInfo): void {
    if (this.nodes.has(agent.id)) { this.updateAgent(agent); return; }

    const isMain = agent.name === "main-actor"
      || agent.agentType === "orchestrator"
      || agent.agentType === "main";

    const node = new CardBabylonNode(agent, this.scene, isMain);
    node.onClick = (info) => {
      document.dispatchEvent(
        new CustomEvent<{ agent: AgentInfo }>("agent-selected", { detail: { agent: info } }),
      );
    };

    this.nodes.set(agent.id, node);
    this.relayout();
  }

  override removeAgent(id: string): void {
    super.removeAgent(id);
    this.relayout();
  }

  /** Recompute grid positions for all cards. */
  private relayout(): void {
    const agents = [...this.nodes.values()];
    const count  = agents.length;
    const cols   = Math.min(count, COLS);
    const rows   = Math.ceil(count / cols);

    // Centre the grid at origin
    const totalW = (cols - 1) * GAP_X;
    const totalH = (rows - 1) * GAP_Y;

    agents.forEach((node, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      node.mesh.position.set(
        col * GAP_X - totalW / 2,
        -(row * GAP_Y - totalH / 2), // top row first
        CARD_DEPTH,
      );
    });
  }
}
