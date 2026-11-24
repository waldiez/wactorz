/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Avatars for where only an agent's name is known: a message's sender, a
 * notification's title.
 *
 * The avatar is found by that name among the agents the page knows. A name
 * nobody has now (an agent since deleted) or that two agents share still gets
 * a character, made up from the name, so no place has a gap where a face
 * should be.
 */
import { loadAvatarPrefs } from "../../config/avatarPrefs";
import { toast } from "../ToastManager";
import { applyAvatarPrefs } from "./appearance";
import { avatar, type AvatarOf } from "./avatar";

/** The avatar of the agent called `name`. */
export type FaceByName = (name: string) => HTMLElement;

/** Avatars by name, looked up among `agents` as they are when asked. */
export function facesByName(agents: () => Iterable<AvatarOf>): FaceByName {
    return name => {
        const named = [...agents()].filter(agent => agent.name === name);
        // Where a name is all there is, the agent has just spoken: the face is its awake one.
        return avatar(named.length === 1 ? named[0]! : { name }, "running", "small");
    };
}

/**
 * Start showing avatars: this browser's choices on the page, and on each
 * notification from an agent the avatar of the one among `agents` it names.
 */
export function showAvatars(agents: () => Iterable<AvatarOf>): void {
    applyAvatarPrefs(loadAvatarPrefs());
    toast.facesFrom(facesByName(agents));
}
