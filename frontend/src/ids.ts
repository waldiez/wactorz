/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */

import { HLCWidGen } from "./vendor/hlcWid";

const gen = new HLCWidGen({ node: "browser", W: 4 });

/** A fresh monotonic WID, optionally namespaced (e.g. `uid("user")` → `user-<wid>`). */
export function uid(prefix?: string): string {
    const wid = gen.next();
    return prefix ? `${prefix}-${wid}` : wid;
}
