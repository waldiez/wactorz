/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * What an agent says of the character drawn for it, as it arrives from the
 * server: read as untrusted, and kept only in the shape the rest of the page
 * expects of it.
 */
import type { AvatarSpec } from "../types/agent";

/** No character names more parts than this. */
const MAX_PARTS = 12;

/**
 * The avatar in `raw`: its parts that are text or a number, by name. Undefined
 * when `raw` is not a dictionary, so a payload that does not speak of an
 * avatar changes nothing.
 */
export function readAvatarSpec(raw: unknown): AvatarSpec | undefined {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
        return undefined;
    }
    const spec: AvatarSpec = {};
    for (const [part, value] of Object.entries(raw).slice(0, MAX_PARTS)) {
        if (typeof value === "string" || (typeof value === "number" && Number.isFinite(value))) {
            spec[part] = value;
        }
    }
    return spec;
}

/** The `avatar` field for an agent's record: what `raw` says, or no field when it says nothing. */
export function avatarField(raw: unknown): { avatar?: AvatarSpec } {
    const avatar = readAvatarSpec(raw);
    return avatar === undefined ? {} : { avatar };
}

/** Whether two agents' avatars say the same, absent and empty alike. */
export function sameAvatar(a: AvatarSpec | undefined, b: AvatarSpec | undefined): boolean {
    return JSON.stringify(a ?? {}) === JSON.stringify(b ?? {});
}
