/**
 * Spawn materialise effect.
 *
 * Plays a flicker-then-solidify animation on a mesh when an agent is spawned.
 * The mesh starts invisible (alpha 0 or scale 0), flickers several times,
 * then fades/scales in to full opacity/size.
 */

import { type AbstractMesh, type Scene } from "@babylonjs/core";

/**
 * Play the spawn effect on `mesh`.
 *
 * @param scene  The active Babylon.js scene.
 * @param mesh   The mesh to animate (should start invisible).
 * @param onDone Optional callback invoked when the effect completes.
 */
export function playSpawnEffect(
  scene: Scene,
  mesh: AbstractMesh,
  onDone?: () => void,
): void {
  mesh.isVisible = false;
  let frame = 0;
  const totalFrames = 60; // ~1 second at 60 fps

  const obs = scene.onBeforeRenderObservable.add(() => {
    frame++;

    // Flicker phase (0–30 frames)
    if (frame < 30) {
      mesh.isVisible = Math.sin(frame * 0.8) > 0;
      return;
    }

    // Scale-in phase (30–60 frames)
    mesh.isVisible = true;
    const progress = (frame - 30) / 30;
    const scale = easeOutBack(progress);
    mesh.scaling.setAll(scale);

    if (frame >= totalFrames) {
      mesh.scaling.setAll(1);
      scene.onBeforeRenderObservable.remove(obs);
      onDone?.();
    }
  });
}

/** Ease-out-back function for a snappy scale-in bounce. */
function easeOutBack(t: number): number {
  const c1 = 1.70158;
  const c3 = c1 + 1;
  return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
}
