/**
 * Heartbeat glow-pulse effect.
 *
 * Plays a subtle emissive intensity pulse on a mesh's material each time
 * a heartbeat message arrives.  The pulse is non-blocking: it uses the
 * scene's `onBeforeRenderObservable` and removes itself when done.
 */

import { StandardMaterial, type Scene } from "@babylonjs/core";

/**
 * Play a single heartbeat pulse on `material`.
 *
 * Temporarily boosts the emissive brightness by `boostFactor`, then
 * restores it over `durationFrames` frames.
 *
 * @param scene          The active Babylon.js scene.
 * @param material       The material to pulse.
 * @param boostFactor    Emissive multiplier at peak (default 2.5).
 * @param durationFrames Total frames for the pulse (default 30).
 */
export function playHeartbeatEffect(
  scene: Scene,
  material: StandardMaterial,
  boostFactor = 2.5,
  durationFrames = 30,
): void {
  const baseColor = material.emissiveColor.clone();
  let frame = 0;

  const obs = scene.onBeforeRenderObservable.add(() => {
    frame++;
    const t = frame / durationFrames;
    // Sine curve: peak at mid, back to 1 at end
    const factor = 1 + (boostFactor - 1) * Math.sin(t * Math.PI);
    material.emissiveColor = baseColor.scale(factor);

    if (frame >= durationFrames) {
      material.emissiveColor = baseColor;
      scene.onBeforeRenderObservable.remove(obs);
    }
  });
}
