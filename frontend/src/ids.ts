/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Client-side id generation, backed by one HLC WID generator per session.
 *
 * WIDs are monotonic and collision-free; raw `Date.now()` collides for events in
 * the same millisecond, and two separate generators sharing a node id could also
 * collide — so all client ids come from this single generator.
 */
import { HLCWidGen } from "@waldiez/wid";

const gen = new HLCWidGen({ node: "browser", W: 4 });

/** A fresh monotonic WID, optionally namespaced (e.g. `uid("user")` → `user-<wid>`). */
export function uid(prefix?: string): string {
    const wid = gen.next();
    return prefix ? `${prefix}-${wid}` : wid;
}
