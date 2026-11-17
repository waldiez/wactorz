/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** The avatar beside each agent's message in a thread: a message names who sent it and nothing more. */
export { facesByName as senderAvatars, type FaceByName as SenderAvatar } from "../avatars";

/** Put `face` before the sender's name on a message's `from` line. */
export function showSender(from: HTMLElement, face: HTMLElement): void {
    from.prepend(face);
}
