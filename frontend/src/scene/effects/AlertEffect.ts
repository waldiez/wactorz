/**
 * Alert shockwave effect.
 *
 * Expands a translucent red ring from the agent's position and fades it out.
 * Severity adjusts colour: error = red, warning = amber.
 */

import {
  Color3,
  Color4,
  Mesh,
  MeshBuilder,
  StandardMaterial,
  Vector3,
  type Scene,
} from "@babylonjs/core";

/**
 * Emit a shockwave ring from `position`.
 *
 * @param scene    The active Babylon.js scene.
 * @param position World position of the originating agent node.
 * @param severity Alert severity level.
 */
export function playAlertEffect(
  scene: Scene,
  position: Vector3,
  severity: "info" | "warning" | "error" | "critical",
): void {
  const color =
    severity === "critical" || severity === "error"
      ? new Color3(1.0, 0.1, 0.1)
      : new Color3(1.0, 0.75, 0.1);

  const ring = MeshBuilder.CreateTorus(
    "alert-ring",
    { diameter: 0.5, thickness: 0.05, tessellation: 40 },
    scene,
  );
  ring.position = position.clone();
  ring.rotation.x = Math.PI / 2;

  const mat = new StandardMaterial("alert-mat", scene);
  mat.emissiveColor = color;
  mat.alpha = 0.8;
  ring.material = mat;

  let frame = 0;
  const obs = scene.onBeforeRenderObservable.add(() => {
    frame++;
    const progress = frame / 50; // ~0.8s effect
    const scale = 1 + progress * 8;
    ring.scaling.setAll(scale);
    mat.alpha = Math.max(0, 0.8 - progress);

    if (frame >= 50) {
      scene.onBeforeRenderObservable.remove(obs);
      mat.dispose();
      ring.dispose();
    }
  });
}
