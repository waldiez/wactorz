/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The Overview's notice of installed packages the server left out, because
 * `WACTORZ_EXTENSIONS` does not name them. Dismissed once for a given list: a
 * package newly left out brings it back.
 */
import { BLOCKED_PACKAGES_KEY, BLOCKED_SEEN_KEY } from "../../config/keys";
import { safeStorage } from "../../safeStorage";
import { button, el, live } from "../dom";

/** The notice's element id, so it is put in the page once. */
export const BLOCKED_NOTICE_ID = "af-blocked-notice";

/** The notice for the packages the server left out, or null when there is nothing to say. */
export function buildBlockedNotice(): HTMLElement | null {
    const packages = safeStorage.get(BLOCKED_PACKAGES_KEY);
    if (!packages || packages === safeStorage.get(BLOCKED_SEEN_KEY)) {
        return null;
    }
    const notice = live(el("div", "af-blocked-notice"), "status", "Packages not loaded");
    notice.id = BLOCKED_NOTICE_ID;
    const text = `Not loaded: ${packages.split(",").join(", ")}. Installed, but not named in WACTORZ_EXTENSIONS.`;
    const dismiss = button("af-blocked-dismiss", "Dismiss");
    dismiss.addEventListener("click", () => {
        safeStorage.set(BLOCKED_SEEN_KEY, packages);
        notice.remove();
    });
    notice.append(el("span", "", text), dismiss);
    return notice;
}
