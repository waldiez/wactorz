/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** What a part of Settings is, for the view that lists and opens it. */

/**
 * Where a section's values are kept. The section says so at its top: a
 * preference changed on a phone does not follow the person to their laptop,
 * and a server value changed here changes it for everyone.
 */
export type SettingsScope = "browser" | "server";

/** One part of Settings, opened at `#/settings/<key>`. */
export interface SettingsSection {
    /** The address's level for it: lower-case, as a view key is. */
    key: string;
    /** Its name, in the list and as its heading. */
    label: string;
    /** One line under its name in the list: what is in it. */
    line: string;
    /** Where what it changes is kept. */
    scope: SettingsScope;
    /** Draw it from the values as they are now. */
    build(): HTMLElement;
    /** Stop whatever the drawn section follows; called before it is drawn again or left. */
    release?(): void;
}

/** What each scope says at the top of its sections. */
export const SCOPE_NOTE: Record<SettingsScope, string> = {
    browser: "Kept in this browser only.",
    server: "Kept on the server, for everyone using this Wactorz.",
};
