/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** The dashboard's Settings: which sections it has, in their order, and what each needs from it. */
import { DEVICES_TAB } from "../../config/hiddenTabs";
import type { IconName } from "../shell/icons";
import { BUILTIN_VIEWS, SETTINGS_VIEW, type View } from "../shell/types";
import type { SettingsSection } from "./section";
import { SettingsView } from "./SettingsView";
import { soundSection } from "./sound";
import { tabsSection, type TabEntry } from "./tabs";

/** What the dashboard's Settings needs from the dashboard. */
export interface DashboardSettingsHost {
    /** The views extensions added, as the header shows them. */
    extraViews(): { key: View; label: string; icon: IconName }[];
    /** Whether Home Assistant is set up, so the Devices link appears at all. */
    haConfigured(): boolean;
    /** A tab was hidden, shown or moved: draw the header again. */
    tabsChanged(): void;
    /** Settings → Budget, which reaches the server through the dashboard's metrics. */
    budget: SettingsSection;
}

/** Every tab the header can show, in its order. */
function tabEntries(extraViews: { key: View; label: string }[]): TabEntry[] {
    return [...BUILTIN_VIEWS, ...extraViews, { key: DEVICES_TAB, label: "Devices" }, SETTINGS_VIEW].map(
        ({ key, label }) => ({ key, label }),
    );
}

/** Settings as the dashboard has it: the spend limit first, where `#/settings` has always opened, then this browser's own. */
export function dashboardSettings(host: DashboardSettingsHost): SettingsView {
    return new SettingsView([
        host.budget,
        tabsSection({
            tabs: () => tabEntries(host.extraViews()),
            haConfigured: () => host.haConfigured(),
            changed: () => host.tabsChanged(),
        }),
        soundSection(),
    ]);
}
