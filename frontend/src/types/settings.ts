/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * What a part of Settings is, for the view that lists and opens it.
 *
 * Kept apart from the view so the event an extension adds a section by
 * (`af-register-settings`) can name it: the event bus imports no view.
 */

/**
 * Where a section's values are kept. The section says so beside its heading:
 * a preference changed on a phone does not follow the person to their laptop,
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
    /** The icon beside its name in the list, by its name in `ui/shell/icons`. */
    icon?: string;
    /** Draw it from the values as they are now. */
    build(): HTMLElement;
    /** Stop whatever the drawn section follows; called before it is drawn again or left. */
    release?(): void;
}
