/**
 * Graph / Neural-Network theme (default).
 *
 * Visual language:
 * - Deep indigo background with a subtle star-field particle system
 * - Agents = glowing spheres with emissive shader + point light halo
 * - MainActor = larger amber/gold sphere pulsing at the scene centre
 * - Edges = LineSystem meshes connecting communicating agents
 * - Layout = simple spring-force simulation updated each frame
 */

import {
  Color3,
  Color4,
  HemisphericLight,
  ParticleSystem,
  PointLight,
  Texture,
  Vector3,
  type Scene,
} from "@babylonjs/core";

import type { AgentInfo } from "../../types/agent";
import { GraphNode } from "../nodes/GraphNode";
import { ThemeBase } from "./ThemeBase";

export class GraphTheme extends ThemeBase {
  readonly name = "graph" as const;

  private ambientLight: HemisphericLight | null = null;
  private starParticles: ParticleSystem | null = null;

  /** Spring-force layout: positions evolve over time. */
  private layoutObserver: ReturnType<Scene["onBeforeRenderObservable"]["add"]> | null = null;

  setup(): void {
    // ── Ambient light ─────────────────────────────────────────────────────────
    this.ambientLight = new HemisphericLight(
      "graph-ambient",
      new Vector3(0, 1, 0),
      this.scene,
    );
    this.ambientLight.intensity = 0.3;
    this.ambientLight.diffuse = new Color3(0.4, 0.6, 1.0);
    this.ambientLight.groundColor = new Color3(0.1, 0.1, 0.3);

    // ── Star-field particles ──────────────────────────────────────────────────
    this.starParticles = new ParticleSystem("stars", 1500, this.scene);
    // Use a blank emitter (particles start at scene origin then spread)
    this.starParticles.emitter = Vector3.Zero();
    this.starParticles.minEmitBox = new Vector3(-40, -20, -40);
    this.starParticles.maxEmitBox = new Vector3(40, 20, 40);
    this.starParticles.color1 = new Color4(0.9, 0.95, 1, 0.8);
    this.starParticles.color2 = new Color4(0.6, 0.7, 1, 0.4);
    this.starParticles.colorDead = new Color4(0, 0, 0.2, 0);
    this.starParticles.minSize = 0.02;
    this.starParticles.maxSize = 0.08;
    this.starParticles.minLifeTime = 999999; // effectively infinite
    this.starParticles.maxLifeTime = 999999;
    this.starParticles.emitRate = 1500;
    this.starParticles.blendMode = ParticleSystem.BLENDMODE_ADD;
    this.starParticles.gravity = Vector3.Zero();
    this.starParticles.minEmitPower = 0;
    this.starParticles.maxEmitPower = 0;
    this.starParticles.updateSpeed = 0;
    this.starParticles.start();

    // ── Force-layout update loop ──────────────────────────────────────────────
    this.layoutObserver = this.scene.onBeforeRenderObservable.add(() => {
      this.stepForceLayout();
    });
  }

  teardown(): void {
    if (this.layoutObserver) {
      this.scene.onBeforeRenderObservable.remove(this.layoutObserver);
      this.layoutObserver = null;
    }
    this.starParticles?.dispose();
    this.starParticles = null;
    this.ambientLight?.dispose();
    this.ambientLight = null;

    // Dispose all nodes
    for (const [id] of this.nodes) {
      this.removeAgent(id);
    }
  }

  addAgent(agent: AgentInfo): void {
    if (this.nodes.has(agent.id)) { this.updateAgent(agent); return; }

    const isMain = agent.name === "main-actor" || agent.agentType === "orchestrator" || agent.agentType === "main";

    // Place new agents at a random position around the scene centre
    const theta = Math.random() * Math.PI * 2;
    const radius = 4 + Math.random() * 8;
    const position = new Vector3(
      Math.cos(theta) * radius,
      (Math.random() - 0.5) * 6,
      Math.sin(theta) * radius,
    );

    // Main actor stays at origin
    const finalPosition = isMain ? Vector3.Zero() : position;

    const node = new GraphNode(agent, this.scene, finalPosition, isMain);
    node.onClick = (info) => {
      document.dispatchEvent(
        new CustomEvent<{ agent: AgentInfo }>("agent-selected", { detail: { agent: info } }),
      );
    };

    this.nodes.set(agent.id, node);
  }

  // ── Force-directed layout ─────────────────────────────────────────────────

  /**
   * Very simple spring-repulsion step (O(n²)).
   * Runs every frame but exits early when agent count is low.
   */
  private stepForceLayout(): void {
    const entries = [...this.nodes.values()] as GraphNode[];
    if (entries.length < 2) return;

    const REPULSION = 2.5;
    const DAMPING = 0.92;
    const CENTRE_GRAVITY = 0.004;

    for (let i = 0; i < entries.length; i++) {
      const a = entries[i];
      if (!a || a.isMainActor) continue; // main actor is pinned

      let fx = 0, fy = 0, fz = 0;

      // Repulsion from other nodes
      for (let j = 0; j < entries.length; j++) {
        if (i === j) continue;
        const b = entries[j];
        if (!b) continue;
        const delta = a.position.subtract(b.position);
        const dist = Math.max(delta.length(), 0.5);
        const force = REPULSION / (dist * dist);
        fx += (delta.x / dist) * force;
        fy += (delta.y / dist) * force;
        fz += (delta.z / dist) * force;
      }

      // Gravity towards centre
      fx -= a.position.x * CENTRE_GRAVITY;
      fy -= a.position.y * CENTRE_GRAVITY;
      fz -= a.position.z * CENTRE_GRAVITY;

      // Update velocity + position
      a.velocity.x = (a.velocity.x + fx) * DAMPING;
      a.velocity.y = (a.velocity.y + fy) * DAMPING;
      a.velocity.z = (a.velocity.z + fz) * DAMPING;

      a.position.x += a.velocity.x;
      a.position.y += a.velocity.y;
      a.position.z += a.velocity.z;
    }
  }
}
