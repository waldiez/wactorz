/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * An agent's avatar as an element: its character on a disc, in the mood its
 * agent is in.
 *
 * It is decoration: the agent's name and its state are always written beside
 * it, so it carries no text and is hidden from a screen reader.
 */
import type { AvatarSpec } from "../../types/agent";
import { el } from "../dom";
import { lookFor, type Mood } from "./cast";
import { avatarUrl } from "./draw";

/** What of an agent decides its character. */
export interface AvatarOf {
    /** Its name, which a character is made up from when nothing else says. */
    name: string;
    /** What the agent says of its avatar. */
    avatar?: AvatarSpec | undefined;
}

/**
 * How large an avatar is drawn: `card` shows the whole character with what it
 * holds; `small` shows its head and shoulders, where a prop could not be read.
 */
export type AvatarSize = "card" | "small";

/** Whose character each avatar shows, so its mood can change without asking again who it is. */
const shown = new WeakMap<HTMLElement, AvatarOf>();

/** Put the avatar `node` in `mood`, keeping whose character it shows. */
export function setAvatarMood(node: HTMLElement, mood: Mood): void {
    const agent = shown.get(node);
    if (agent && node.dataset["mood"] !== mood) {
        paintAvatar(node, agent, mood);
    }
}

/** Show on `node`, an avatar this module made, the character of `agent` in `mood`. */
export function paintAvatar(node: HTMLElement, agent: AvatarOf, mood: Mood): void {
    shown.set(node, { name: agent.name, avatar: agent.avatar });
    const crop = node.classList.contains("af-avatar-card") ? "full" : "head";
    const url = avatarUrl(lookFor(agent.name, agent.avatar, mood), crop);
    const img = node.querySelector("img");
    // Left alone when it already shows this: setting it again would decode the image again.
    if (img && img.getAttribute("src") !== url) {
        img.src = url;
    }
    node.dataset["mood"] = mood;
}

/** The avatar of `agent` in `mood`, at `size`. */
export function avatar(agent: AvatarOf, mood: Mood, size: AvatarSize): HTMLElement {
    const node = el("span", `af-avatar af-avatar-${size}`);
    node.setAttribute("aria-hidden", "true");
    const img = el("img");
    img.alt = "";
    img.decoding = "async";
    node.appendChild(img);
    paintAvatar(node, agent, mood);
    return node;
}
