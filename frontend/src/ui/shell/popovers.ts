/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Settings popovers for the dashboard bottom bar.
 *
 * Both builders are self-contained (they only touch the audio/TTS singletons
 * and the reset REST endpoint), so they live outside the CardDashboard class.
 */
import { ambient, AMBIENT_TRACKS } from "../../io/AmbientManager";
import { voiceControls } from "../../io/voiceControls";
import { requestReset } from "../../io/resetApi";
import { toast } from "../ToastManager";
import { listen } from "../../events";
import { parseRoute, type Route } from "../../router";
import { button, el, named, option } from "../dom";
import { sayRefusal } from "./mutate";
import { AGENT_SCOPES, buildScopeSwitch, nodesSay, openAgent } from "./resetScope";

/** Beep + TTS toggle buttons; the TTS toggle shows/hides `voiceRow`. */
function buildAudioToggles(voiceRow: HTMLElement): HTMLElement {
    const toggleRow = el("div", "af-audio-row");

    const beepBtn = button(`af-audio-toggle${voiceControls().beepEnabled ? " on" : ""}`, "🔔 Beep");
    beepBtn.title = "Notification beep";
    beepBtn.addEventListener("click", () => {
        beepBtn.classList.toggle("on", voiceControls().toggleBeep());
    });

    const ttsBtn = button(`af-audio-toggle${voiceControls().ttsEnabled ? " on" : ""}`, "🗣 TTS");
    ttsBtn.title = "Read replies aloud";
    ttsBtn.addEventListener("click", () => {
        const on = voiceControls().toggleTTS();
        ttsBtn.classList.toggle("on", on);
        voiceRow.style.display = on ? "" : "none";
    });

    toggleRow.append(beepBtn, ttsBtn);
    return toggleRow;
}

/** Voice-select row, repopulated when the browser's voice list loads.
 *  Returns the row plus a `release` that unhooks the `tts-voices-loaded`
 *  listener — the audio popover is rebuilt on every nav rebuild, and without
 *  this each rebuild would stack another listener mutating a detached select. */
function buildVoiceRow(): { row: HTMLElement; release: () => void } {
    const voiceRow = el("div", "af-audio-row");
    voiceRow.style.display = voiceControls().ttsEnabled ? "" : "none";

    const voiceSel = named(el("select", "af-audio-select"), "tts-voice", "TTS voice", "af-tts-voice");
    voiceSel.title = "TTS voice";
    voiceSel.appendChild(option("", "— loading voices… —"));

    const populateVoices = (): void => {
        const voices = voiceControls().voices;
        if (!voices.length) {
            return;
        }
        while (voiceSel.options.length > 1) {
            voiceSel.remove(1);
        }
        voices.forEach(v => {
            voiceSel.appendChild(
                option(v.name, v.name.replace(/^Microsoft\s+/, "").replace(/\s+Online.*$/i, "")),
            );
        });
        const saved = voiceControls().selectedVoice;
        if (saved) {
            voiceSel.value = saved;
        }
    };

    populateVoices();
    const stopFollowing = listen("tts-voices-loaded", () => populateVoices());
    voiceSel.addEventListener("change", () => voiceControls().setVoice(voiceSel.value));

    voiceRow.appendChild(voiceSel);
    return { row: voiceRow, release: stopFollowing };
}

/** Ambient volume slider row (visibility toggled by the track buttons). */
function buildVolumeRow(): HTMLElement {
    const volRow = el("div", "af-audio-row af-audio-vol-row");
    volRow.style.display = ambient.track === "none" ? "none" : "";

    const volIcon = el("span", "", "🔉");
    volIcon.style.fontSize = "14px";

    const volSlider = named(el("input", "af-audio-slider"), "ambient-volume", "Ambient volume");
    volSlider.type = "range";
    volSlider.min = "0";
    volSlider.max = "1";
    volSlider.step = "0.05";
    volSlider.value = String(ambient.volume);
    volSlider.addEventListener("input", () => ambient.setVolume(parseFloat(volSlider.value)));

    volRow.append(volIcon, volSlider);
    return volRow;
}

/** Ambient track buttons; selecting one updates the volume row's visibility. */
function buildAmbientTracks(volRow: HTMLElement): HTMLElement {
    const trackRow = el("div", "af-audio-tracks");

    AMBIENT_TRACKS.forEach(({ id, label }) => {
        const btn = button(`af-audio-track-btn${ambient.track === id ? " on" : ""}`, label);
        btn.addEventListener("click", () => {
            trackRow.querySelectorAll(".af-audio-track-btn").forEach(b => b.classList.remove("on"));
            btn.classList.add("on");
            ambient.setTrack(id);
            volRow.style.display = id === "none" ? "none" : "";
        });
        trackRow.appendChild(btn);
    });
    return trackRow;
}

/** Ambient track buttons + volume slider (volume hidden when track is "none"). */
function buildAmbientRows(): DocumentFragment {
    const frag = document.createDocumentFragment();

    const trackLabel = el("div", "af-audio-label", "Ambient");
    frag.appendChild(trackLabel);

    const volRow = buildVolumeRow();
    frag.appendChild(buildAmbientTracks(volRow));
    frag.appendChild(volRow);
    return frag;
}

/** An audio popover carrying teardown for its `tts-voices-loaded` listener
 *  (invoked by `releaseHeaderPopovers` before the element is removed). */
export interface AudioPopover extends HTMLElement {
    release: () => void;
}

/** Audio controls: beep/TTS toggles, voice select, ambient track + volume. */
export function buildAudioPopover(): AudioPopover {
    const pop = el("div", "af-audio-popover glass");

    const { row: voiceRow, release } = buildVoiceRow();
    pop.appendChild(buildAudioToggles(voiceRow));
    pop.appendChild(voiceRow);

    pop.appendChild(el("div", "af-audio-divider"));

    pop.appendChild(buildAmbientRows());
    return Object.assign(pop, { release: release });
}

const RESET_ICONS: Record<string, string> = {
    chat: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9.5a5 5 0 0 1-5 5H3l-2 2V5a5 5 0 0 1 5-5h3"/><circle cx="12" cy="4" r="3"/></svg>`,
    metrics: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="9" width="3" height="6" rx="1"/><rect x="6" y="5" width="3" height="10" rx="1"/><rect x="11" y="2" width="3" height="13" rx="1"/></svg>`,
    spawns: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="2" r="1.5"/><circle cx="2" cy="13" r="1.5"/><circle cx="14" cy="13" r="1.5"/><path d="M8 3.5v4m0 4-5 3.5m5-3.5 5 3.5m-5-7.5-5 3.5m5-3.5 5 3.5"/></svg>`,
    state: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="12" height="3" rx="1"/><rect x="2" y="8" width="12" height="3" rx="1"/><rect x="2" y="13" width="8" height="2" rx="1"/></svg>`,
    logs: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 2h10a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z"/><path d="M5 6h6M5 9h4"/></svg>`,
    all: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4h12M5 4V2h6v2M6 7v5M10 7v5M3 4l1 9a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l1-9"/></svg>`,
};

const RESET_SCOPES: { scope: string; label: string; danger?: boolean }[] = [
    // Says "& files" because clearing the history deletes the attachments sent
    // with it — the rows being cleared are the only thing that referenced them.
    { scope: "chat", label: "Chat history & files" },
    { scope: "metrics", label: "Metrics & costs" },
    { scope: "spawns", label: "Spawn registry" },
    // Restarts it: what an agent read from its state at start is in memory, and
    // only a fresh start forgets it.
    { scope: "state", label: "Agent state (restarts it)" },
    // This server's: a node's own output goes to its journal or its container,
    // which the node cannot clear and the dashboard never shows.
    { scope: "logs", label: "This server's log files" },
    { scope: "all", label: "Wipe everything", danger: true },
];

/** Fire the reset REST call for a scope and toast the outcome. */
async function postReset(
    scope: string,
    label: string,
    pop: HTMLElement,
    agent: string | null,
): Promise<void> {
    pop.classList.remove("open");
    const got = await requestReset(scope, agent);
    if (got.ok) {
        const whom = agent ? ` for @${agent}` : "";
        const missed = nodesSay(got.value?.nodes);
        // A warning when some node kept its copy: the reset is only partly done.
        toast.show({
            type: missed ? "alert-warning" : "system",
            title: "Reset",
            message: `${label} cleared${whom}.${missed}`,
        });
    } else {
        sayRefusal(got, "Reset failed", "Reset unconfirmed");
    }
}

/** One armed-then-fire reset button; pushes its disarm fn into `armResets`. */
function buildResetButton(
    pop: HTMLElement,
    armResets: Array<() => void>,
    { scope, label, danger }: (typeof RESET_SCOPES)[number],
    agent: string | null,
): HTMLButtonElement {
    const btn = button("af-mini-btn");
    btn.style.cssText = [
        "display:flex;align-items:center;gap:8px;width:100%;",
        "padding:6px 8px;margin-bottom:3px;border-radius:6px;",
        "font-size:12px;text-align:left;transition:background .15s;",
        danger ? "color:#f87171;" : "",
    ].join("");
    // The icon is fixed markup; the label is kept as its own element so the
    // confirm step can rewrite it without reparsing the button.
    btn.innerHTML = RESET_ICONS[scope] ?? "";
    const span = el("span", "", label);
    btn.appendChild(span);
    let armed = false;
    let armTimer: ReturnType<typeof setTimeout> | null = null;

    const disarm = () => {
        if (armTimer) {
            clearTimeout(armTimer);
            armTimer = null;
        }
        armed = false;
        span.textContent = label;
        btn.style.background = "";
    };
    armResets.push(disarm);

    btn.addEventListener("click", () => {
        // Two-step confirm: first click arms, second fires.
        if (!armed) {
            armResets.forEach(fn => fn !== disarm && fn());
            armed = true;
            span.textContent = `Confirm ${label.toLowerCase()}?`;
            btn.style.background = danger ? "rgba(248,113,113,.15)" : "rgba(255,255,255,.1)";
            armTimer = setTimeout(disarm, 3000);
            return;
        }
        disarm();
        void postReset(scope, label, pop, agent);
    });

    return btn;
}

/** A reset popover that exposes a hook to re-arm its two-step confirm buttons
 *  (called when the popover is re-opened, so a previously-armed button resets). */
export interface ResetPopover extends HTMLElement {
    /** Disarm every armed button (called when the popover is dismissed). */
    _resetArmed(): void;
    /** Stop following the route, before the popover is removed. */
    release(): void;
}

/**
 * Clear stored state: for the agent whose conversation is open, or for all.
 *
 * Rebuilt whenever the open agent changes, so the choice it offers is always
 * about the agent on screen. The rows that cannot be limited to one agent are
 * left out while one is chosen, rather than shown and quietly applied to all.
 */
export function buildResetPopover(): ResetPopover {
    const pop = el("div", "af-audio-popover glass");
    pop.style.cssText = "min-width:210px;padding:12px 14px;";
    const armResets: Array<() => void> = [];
    let agent = openAgent(parseRoute(location.hash));
    let forAgent = true;

    const render = (): void => {
        armResets.splice(0).forEach(disarm => disarm());
        pop.replaceChildren();
        const title = el("div", "", "Clear stored state");
        title.style.cssText =
            "font-size:10px;font-weight:600;opacity:.45;margin-bottom:10px;text-transform:uppercase;letter-spacing:.08em;";
        pop.appendChild(title);
        if (agent) {
            pop.appendChild(
                buildScopeSwitch(agent, chosen => {
                    forAgent = chosen;
                    render();
                }),
            );
            const picked = pop.querySelector<HTMLInputElement>(
                `input[value="${forAgent ? "agent" : "all"}"]`,
            );
            if (picked) {
                picked.checked = true;
            }
        }
        const whom = agent && forAgent ? agent : null;
        for (const spec of RESET_SCOPES) {
            if (whom && !AGENT_SCOPES.has(spec.scope)) {
                continue;
            }
            if (spec.danger) {
                const hr = el("div");
                hr.style.cssText = "height:1px;background:rgba(255,255,255,.08);margin:6px 0 8px;";
                pop.appendChild(hr);
            }
            pop.appendChild(buildResetButton(pop, armResets, spec, whom));
        }
    };
    const follow = (route: Route): void => {
        const next = openAgent(route);
        if (next !== agent) {
            agent = next;
            forAgent = true;
            render();
        }
    };
    const following = [listen("af-route", follow), listen("af-view-changed", follow)];
    render();
    return Object.assign(pop, {
        _resetArmed: () => armResets.forEach(fn => fn()),
        release: () => following.forEach(stop => stop()),
    });
}
