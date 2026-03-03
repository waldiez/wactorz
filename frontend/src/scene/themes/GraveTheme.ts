/**
 * Π-απαθάνατοι — "Papadeath" graveyard theme.
 *
 * Visual language:
 * - Near-pitch-black sky with eerie green/purple nebula fog
 * - MainActor = the Boss skull: pulsing crimson halo
 * - Agents = teetering tombstones with sickly neon-green glow, slow bob
 * - Spring-force layout (same as GraphTheme but slower, more spread)
 * - Particle fog rising from the "ground"
 * - Directed orange/purple light from below (cemetary uplighting)
 */

import {
  Color3,
  Color4,
  DirectionalLight,
  DynamicTexture,
  HemisphericLight,
  Mesh,
  MeshBuilder,
  ParticleSystem,
  StandardMaterial,
  Vector3,
  type Scene,
} from "@babylonjs/core";

import type { AgentInfo } from "../../types/agent";
import { GraveNode } from "../nodes/GraveNode";
import { ThemeBase } from "./ThemeBase";

/** How long (ms) ash markers linger before fully vanishing. */
const ASH_LINGER_MS = 5 * 60 * 1000; // 5 minutes

const REPULSION  = 28;
const GRAVITY    = 0.04;
const DAMPING    = 0.88;
const MAX_SPEED  = 0.25;

/** A fading ash marker left where a deleted agent once stood. */
interface AshMarker {
  stone: Mesh;
  mat:   StandardMaterial;
  timer: ReturnType<typeof setTimeout>;
  obs:   ReturnType<Scene["onBeforeRenderObservable"]["add"]>;
  age:   number;        // frames since creation
}

export class GraveTheme extends ThemeBase {
  readonly name = "grave" as const;

  private ambient:    HemisphericLight | null = null;
  private crypt:      DirectionalLight  | null = null;
  private fog:        ParticleSystem    | null = null;
  private layoutObs:  ReturnType<Scene["onBeforeRenderObservable"]["add"]> | null = null;

  /** Ash markers keyed by the dead agent's id. */
  private ashes = new Map<string, AshMarker>();

  setup(): void {
    // Midnight graveyard sky
    this.scene.clearColor = new Color4(0.02, 0.04, 0.02, 1);

    // Dim, sickly-green ambient
    this.ambient = new HemisphericLight("grave-ambient", new Vector3(0, 1, 0), this.scene);
    this.ambient.intensity  = 0.3;
    this.ambient.diffuse    = new Color3(0.15, 0.55, 0.15);
    this.ambient.groundColor = new Color3(0.08, 0.05, 0.08);

    // Upward crypt light (eerie underlighting)
    this.crypt = new DirectionalLight("grave-crypt", new Vector3(0, 1, 0.3), this.scene);
    this.crypt.diffuse    = new Color3(0.45, 0.1, 0.55);
    this.crypt.intensity  = 0.6;

    // Rising grave fog
    this.fog = new ParticleSystem("grave-fog", 1200, this.scene);
    this.fog.emitter    = Vector3.Zero();
    this.fog.minEmitBox = new Vector3(-30, -1, -30);
    this.fog.maxEmitBox = new Vector3(30,  -0.5, 30);
    this.fog.color1     = new Color4(0.1, 0.35, 0.1, 0.07);
    this.fog.color2     = new Color4(0.25, 0.1, 0.35, 0.05);
    this.fog.colorDead  = new Color4(0, 0, 0, 0);
    this.fog.minSize    = 2.5;
    this.fog.maxSize    = 7.0;
    this.fog.minLifeTime = 8;
    this.fog.maxLifeTime = 18;
    this.fog.emitRate   = 80;
    this.fog.minEmitPower = 0.1;
    this.fog.maxEmitPower = 0.3;
    this.fog.direction1  = new Vector3(-0.2, 1, -0.2);
    this.fog.direction2  = new Vector3(0.2, 1, 0.2);
    this.fog.gravity     = new Vector3(0, 0.05, 0);
    this.fog.blendMode   = ParticleSystem.BLENDMODE_ADD;
    this.fog.start();

    // Spring-force layout
    this.layoutObs = this.scene.onBeforeRenderObservable.add(() => this.stepLayout());
  }

  teardown(): void {
    if (this.layoutObs) {
      this.scene.onBeforeRenderObservable.remove(this.layoutObs);
      this.layoutObs = null;
    }
    this.fog?.dispose();   this.fog = null;
    this.crypt?.dispose(); this.crypt = null;
    this.ambient?.dispose(); this.ambient = null;

    for (const [id] of this.nodes) super.removeAgent(id); // skip ash on full teardown
    for (const [id] of this.ashes) this.disposeAsh(id);

    this.scene.clearColor = new Color4(0.02, 0.03, 0.10, 1);
  }

  addAgent(agent: AgentInfo): void {
    if (this.nodes.has(agent.id)) { this.updateAgent(agent); return; }

    const isMain = agent.name === "main-actor"
      || agent.agentType === "orchestrator"
      || agent.agentType === "main";

    const pos = isMain
      ? Vector3.Zero()
      : new Vector3(
          (Math.random() - 0.5) * 16,
          0,
          (Math.random() - 0.5) * 16,
        );

    const node = new GraveNode(agent, this.scene, pos, isMain);
    node.onClick = (info) => {
      document.dispatchEvent(
        new CustomEvent<{ agent: AgentInfo }>("agent-selected", { detail: { agent: info } }),
      );
    };

    this.nodes.set(agent.id, node);
  }

  /** Override: instead of removing the node cleanly, turn it to crumbling ashes. */
  override removeAgent(id: string): void {
    const node = this.nodes.get(id) as GraveNode | undefined;
    if (!node) { super.removeAgent(id); return; }

    const pos = node.mesh.position.clone();
    const name = node.agentName ?? id.slice(-6);
    super.removeAgent(id); // dispose 3D node

    this.spawnAsh(id, name, pos);
  }

  private spawnAsh(id: string, agentName: string, pos: Vector3): void {
    // Crumbled tombstone slab (flat, grey, barely above ground)
    const stone = MeshBuilder.CreateBox(`ash-${id}`, { width: 0.5, height: 0.08, depth: 0.35 }, this.scene);
    stone.position.set(pos.x + (Math.random() - 0.5) * 0.3, 0.04, pos.z + (Math.random() - 0.5) * 0.3);
    stone.rotation.y = Math.random() * Math.PI;
    stone.rotation.x = (Math.random() - 0.5) * 0.3; // slightly tilted

    // RIP label texture
    const tex = new DynamicTexture(`ash-tex-${id}`, { width: 128, height: 64 }, this.scene);
    const ctx = tex.getContext();
    ctx.fillStyle = "#1a1a1a";
    ctx.fillRect(0, 0, 128, 64);
    ctx.fillStyle = "#445544";
    ctx.font = "bold 13px monospace";
    ctx.fillText("R.I.P.", 32, 22);
    ctx.fillStyle = "#334433";
    ctx.font = "10px monospace";
    ctx.fillText(agentName.slice(0, 14), 8, 40);
    tex.update();

    const mat = new StandardMaterial(`ash-mat-${id}`, this.scene);
    mat.diffuseTexture  = tex;
    mat.diffuseColor    = new Color3(0.18, 0.22, 0.18);
    mat.emissiveColor   = new Color3(0.03, 0.07, 0.03);
    mat.alpha           = 0.85;
    stone.material      = mat;

    // Fade out over ASH_LINGER_MS
    const obs = this.scene.onBeforeRenderObservable.add(() => {
      const ash = this.ashes.get(id);
      if (!ash) return;
      ash.age++;
      // Slow flicker as it disintegrates
      mat.alpha = 0.85 * (1 - ash.age / 18000) + 0.05 * Math.sin(ash.age * 0.05);
    });

    const timer = setTimeout(() => this.disposeAsh(id), ASH_LINGER_MS);

    this.ashes.set(id, { stone, mat, timer, obs, age: 0 });
  }

  private disposeAsh(id: string): void {
    const ash = this.ashes.get(id);
    if (!ash) return;
    this.scene.onBeforeRenderObservable.remove(ash.obs);
    clearTimeout(ash.timer);
    ash.stone.dispose();
    ash.mat.dispose();
    this.ashes.delete(id);
  }

  private stepLayout(): void {
    const nodes = [...this.nodes.values()] as GraveNode[];

    // Repulsion between all pairs
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i]!;
        const b = nodes[j]!;
        if (a.isMainActor || b.isMainActor) continue;

        const dx = a.mesh.position.x - b.mesh.position.x;
        const dz = a.mesh.position.z - b.mesh.position.z;
        const dist = Math.sqrt(dx * dx + dz * dz) + 0.001;
        const force = REPULSION / (dist * dist);

        a.velocity.x += (dx / dist) * force;
        a.velocity.z += (dz / dist) * force;
        b.velocity.x -= (dx / dist) * force;
        b.velocity.z -= (dz / dist) * force;
      }
    }

    // Gravity toward origin, apply velocity
    for (const node of nodes) {
      if (node.isMainActor) continue;

      node.velocity.x -= node.mesh.position.x * GRAVITY;
      node.velocity.z -= node.mesh.position.z * GRAVITY;
      node.velocity.x *= DAMPING;
      node.velocity.z *= DAMPING;

      const speed = Math.sqrt(node.velocity.x ** 2 + node.velocity.z ** 2);
      if (speed > MAX_SPEED) {
        node.velocity.x = (node.velocity.x / speed) * MAX_SPEED;
        node.velocity.z = (node.velocity.z / speed) * MAX_SPEED;
      }

      node.mesh.position.x += node.velocity.x;
      node.mesh.position.z += node.velocity.z;
      node.setBasePosition(node.mesh.position.x, node.mesh.position.z);
    }
  }
}
