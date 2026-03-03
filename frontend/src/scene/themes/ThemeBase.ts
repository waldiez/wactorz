/**
 * Abstract base for AgentFlow visual themes.
 *
 * Each theme is responsible for:
 * - Building its scene environment (skybox, lights, particles)
 * - Creating/updating/removing agent nodes
 * - Reacting to runtime events (heartbeat, alert, spawn)
 *
 * Themes must be side-effect-free on construction; actual scene setup
 * happens in {@link setup}, and cleanup in {@link teardown}.
 */

import type { Scene } from "@babylonjs/core";
import type { AgentInfo } from "../../types/agent";
import type { AgentNodeBase } from "../nodes/AgentNodeBase";
import { playMessageEffect } from "../effects/MessageEffect";

export abstract class ThemeBase {
  /** Unique theme identifier used by {@link SceneManager}. */
  abstract readonly name: "graph" | "galaxy" | "cards" | "cards-3d" | "grave" | "social" | "fin" | "ops";

  /** The nodes currently in this theme, keyed by agent ID. */
  protected nodes: Map<string, AgentNodeBase> = new Map();

  constructor(protected readonly scene: Scene) {}

  /** Initialise scene environment (skybox, lights, particles). */
  abstract setup(): void;

  /** Remove all theme-specific scene objects. */
  abstract teardown(): void;

  // ── Node lifecycle ──────────────────────────────────────────────────────────

  /** Create and add a new agent node to the scene. */
  abstract addAgent(agent: AgentInfo): void;

  /** Update an existing agent node (state change, name change, etc.). */
  updateAgent(agent: AgentInfo): void {
    this.nodes.get(agent.id)?.onStateChange(agent.state);
  }

  /** Remove an agent node from the scene. */
  removeAgent(id: string): void {
    const node = this.nodes.get(id);
    if (node) {
      node.dispose();
      this.nodes.delete(id);
    }
  }

  // ── Event reactions ─────────────────────────────────────────────────────────

  /** Trigger a heartbeat visual (e.g. glow pulse) on the given agent node. */
  onHeartbeat(agentId: string): void {
    this.nodes.get(agentId)?.pulseHeartbeat();
  }

  /** Trigger an alert visual (e.g. red shockwave) on the given agent node. */
  onAlert(agentId: string, severity: string): void {
    this.nodes.get(agentId)?.showAlert(severity);
  }

  /** Trigger a spawn materialise effect on the given agent node. */
  onSpawn(agentId: string): void {
    this.nodes.get(agentId)?.playSpawnEffect();
  }

  /** Animate a message-comet arc between two agent nodes. */
  onChat(fromId: string, toId: string): void {
    const from = this.nodes.get(fromId);
    const to = this.nodes.get(toId);
    if (from && to) {
      playMessageEffect(this.scene, from.position.clone(), to.position.clone());
    }
  }

  /** Return all currently tracked nodes. */
  getNodes(): IterableIterator<AgentNodeBase> {
    return this.nodes.values();
  }

  /** Look up a single node by agent ID. */
  getNode(agentId: string): AgentNodeBase | undefined {
    return this.nodes.get(agentId);
  }
}
