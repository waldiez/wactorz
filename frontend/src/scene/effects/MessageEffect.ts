/**
 * Message-transfer effect.
 *
 * Animates an electric arc / comet travelling from one agent node to another.
 * The comet is a small emissive sphere that follows a bezier path between
 * the source and destination positions.
 */

import {
  Color3,
  Mesh,
  MeshBuilder,
  StandardMaterial,
  Vector3,
  type Scene,
} from "@babylonjs/core";

/**
 * Animate a message comet from `from` to `to`.
 *
 * @param scene The active Babylon.js scene.
 * @param from  World position of the sender.
 * @param to    World position of the receiver.
 */
export function playMessageEffect(
  scene: Scene,
  from: Vector3,
  to: Vector3,
): void {
  const comet = MeshBuilder.CreateSphere(
    "msg-comet",
    { diameter: 0.18, segments: 8 },
    scene,
  );

  const mat = new StandardMaterial("msg-mat", scene);
  mat.emissiveColor = new Color3(0.4, 0.9, 1.0);
  mat.disableLighting = true;
  comet.material = mat;
  comet.position = from.clone();

  // Quadratic bezier control point: arc midpoint lifted vertically
  const mid = Vector3.Lerp(from, to, 0.5);
  mid.y += Vector3.Distance(from, to) * 0.4;

  let t = 0;
  const obs = scene.onBeforeRenderObservable.add(() => {
    t = Math.min(t + 0.025, 1);

    // Quadratic bezier: B(t) = (1-t)²·P0 + 2(1-t)t·P1 + t²·P2
    const oneMinusT = 1 - t;
    comet.position.x =
      oneMinusT * oneMinusT * from.x + 2 * oneMinusT * t * mid.x + t * t * to.x;
    comet.position.y =
      oneMinusT * oneMinusT * from.y + 2 * oneMinusT * t * mid.y + t * t * to.y;
    comet.position.z =
      oneMinusT * oneMinusT * from.z + 2 * oneMinusT * t * mid.z + t * t * to.z;

    // Fade out as comet approaches destination
    mat.alpha = t > 0.8 ? 1 - (t - 0.8) * 5 : 1;

    if (t >= 1) {
      scene.onBeforeRenderObservable.remove(obs);
      mat.dispose();
      comet.dispose();
    }
  });
}
