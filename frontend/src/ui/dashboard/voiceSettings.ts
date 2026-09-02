/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * What this deployment does about speech, and the one control that needs a page.
 *
 * Which branch listens and which speaks can be changed here. The environment sets
 * what this starts as, and a choice made here supersedes it and is kept, so a
 * restart comes back to what was chosen rather than what was configured. That is
 * why there is a way back: "Reset to configured" drops the choices.
 *
 * Where the services live cannot. `WACTORZ_STT_URI` and `WACTORZ_TTS_URI` name
 * things this process opens connections to, and a setting a browser can write is
 * one it can point at anything reachable from the machine. Those stay with the
 * machine they were configured on, and are not shown here either.
 *
 * `host` also has its control here, because it has one nowhere else: nobody is
 * necessarily at a screen, so the one place it can be driven from is a page
 * someone deliberately opened.
 */

import { sttMode, sttAvailable, sttLive, canRecognise } from "../../ext/stt";
import { ttsMode, ttsAvailable, tts, ttsVoice } from "../../ext/tts";
import { toast } from "../ToastManager";

/** How this deployment listens, said plainly. */
export function listeningSays(): string {
    const mode = sttMode();
    if (mode === "off") {
        return "Nothing is listening.";
    }
    if (mode === "browser") {
        return canRecognise()
            ? "This browser listens and recognises for itself; the audio goes to its vendor, not here."
            : "The browser would listen for itself, but this one cannot — Chromium over localhost or TLS.";
    }
    if (mode === "host") {
        return "This machine listens through its own microphone.";
    }
    if (!sttAvailable()) {
        return "The microphone is configured, but no recogniser can be reached.";
    }
    return sttLive()
        ? "The browser listens, and words appear as they are spoken."
        : "The browser listens, and the words arrive when you stop.";
}

/** How this deployment speaks, said plainly. */
export function speakingSays(): string {
    const mode = ttsMode();
    if (mode === "off") {
        return "Nothing is read aloud.";
    }
    if (mode === "browser") {
        return "This browser reads replies in its own voice; the text is sent nowhere.";
    }
    if (mode === "host") {
        return "This machine reads replies aloud through its own speakers.";
    }
    return ttsAvailable()
        ? "Replies are spoken by the server and played here."
        : "Replies would be spoken by the server, but it has no synthesiser.";
}

function detailLine(detail: string): HTMLElement {
    const said = document.createElement("p");
    said.className = "af-voice-detail";
    said.textContent = detail;
    return said;
}

/**
 * Ask this machine whether its microphone works.
 *
 * Hears without acting: this answers "is the device plugged in and pointing at
 * the room", which is a hardware question. Routing what it heard would put the
 * answer through a model and leave a row in the chat log, so a check would cost
 * a turn and read like a conversation nobody started.
 */
async function checkMicrophone(btn: HTMLButtonElement, apiBase: string): Promise<void> {
    btn.disabled = true;
    const was = btn.textContent;
    btn.textContent = "Listening…";
    try {
        // Generous, because listening legitimately takes many seconds -- but
        // bounded, because a connection that never answers would otherwise leave
        // the button saying "Listening…" for the life of the page.
        const res = await fetch(`${apiBase}/api/stt/listen`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ act: false }),
            signal: AbortSignal.timeout(LISTEN_TIMEOUT_MS),
        });
        const body = (await res.json()) as {
            text?: string;
            heard?: boolean;
            speaking?: boolean;
            error?: string;
        };
        if (!res.ok) {
            toast.show({ type: "alert-error", title: "Could not listen", message: body.error ?? "" });
        } else if (body.speaking) {
            // Not silence: it declines while the room is being spoken to, so that
            // it does not hear the reply and answer it.
            toast.show({ type: "alert-warning", title: "Still speaking", message: "Try again in a moment." });
        } else if (body.heard) {
            toast.show({ type: "system", title: "Microphone works", message: body.text ?? "" });
        } else {
            toast.show({
                type: "system",
                title: "Nothing heard",
                message: "The microphone opened, but nobody spoke.",
            });
        }
    } catch {
        toast.show({ type: "alert-error", title: "Could not listen", message: "The server did not answer." });
    } finally {
        btn.disabled = false;
        btn.textContent = was;
    }
}

/** How long to wait on one turn: the recording, and then reading it back. */
const LISTEN_TIMEOUT_MS = 120_000;

/** The branches, in the order they read. */
const STT_BRANCHES = ["off", "browser", "server", "host"] as const;
const TTS_BRANCHES = ["off", "browser", "server", "host"] as const;

/** Change one branch, and say so if the server will not have it. */
async function chooseBranch(setting: string, value: string, apiBase: string): Promise<boolean> {
    try {
        const res = await fetch(`${apiBase}/api/voice`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ [setting]: value }),
        });
        if (!res.ok) {
            const body = (await res.json()) as { error?: string };
            toast.show({
                type: "alert-error",
                title: "Could not change that",
                message: body.error ?? "",
            });
            return false;
        }
        return true;
    } catch {
        toast.show({
            type: "alert-error",
            title: "Could not change that",
            message: "The server did not answer.",
        });
        return false;
    }
}

/** A select for one branch, which applies the moment it is changed. */
function buildBranchPicker(
    label: string,
    setting: string,
    current: string,
    branches: readonly string[],
    says: string,
    apiBase: string,
    onChanged: () => void,
): HTMLElement {
    const group = document.createElement("div");
    group.className = "af-voice-group";

    const row = document.createElement("div");
    row.className = "af-voice-row";

    const name = document.createElement("span");
    name.className = "af-voice-term";
    name.textContent = label;

    const select = document.createElement("select");
    select.className = "af-audio-select";
    select.setAttribute("aria-label", label);
    for (const branch of branches) {
        const option = document.createElement("option");
        option.value = branch;
        option.textContent = branch;
        option.selected = branch === current;
        select.appendChild(option);
    }
    select.addEventListener("change", () => {
        const wanted = select.value;
        select.disabled = true;
        void chooseBranch(setting, wanted, apiBase).then(ok => {
            select.disabled = false;
            // Redrawn either way: on success so the rest of the section follows
            // the new branch, and on failure so the select returns to the truth.
            onChanged();
            if (!ok) {
                select.value = current;
            }
        });
    });

    row.append(name, select);
    group.append(row, detailLine(says));
    return group;
}

/**
 * Which voice this deployment speaks in.
 *
 * Distinct from the one in the audio popover, which is this browser's own and
 * lives in its local storage. On `host` nobody is at a browser -- the machine
 * speaks into a room -- so the choice has to belong to the deployment, and this
 * is the only control that sets it.
 *
 * Read once rather than subscribed to: this section is rebuilt whenever the view
 * is, and a listener per build with nothing to release it leaks. By the time
 * anyone opens Settings the list has long since loaded, and a list that has not
 * says so and fills on reopening.
 */
function voiceOptions(voices: readonly { name: string }[], current: string): HTMLSelectElement {
    const select = document.createElement("select");
    select.className = "af-audio-select";
    select.setAttribute("aria-label", "Voice");
    select.disabled = !voices.length;

    const configured = document.createElement("option");
    configured.value = "";
    // An empty list is an answer, not a list still loading: a named synthesiser
    // has voices of its own that this deployment cannot enumerate.
    configured.textContent = voices.length ? "— as configured —" : "— chosen by the service —";
    select.appendChild(configured);

    for (const voice of voices) {
        const option = document.createElement("option");
        option.value = voice.name;
        option.textContent = voice.name.replace(/^Microsoft\s+/, "").replace(/\s+Online.*$/i, "");
        select.appendChild(option);
    }
    // Set once the options exist, and only for a voice this list actually has:
    // a control that cannot say what the setting currently is reads as though
    // nothing were chosen, and this choice outlives a restart.
    if (current) {
        select.value = current;
    }
    return select;
}

function buildVoicePicker(apiBase: string, onChanged: () => void): HTMLElement {
    const group = document.createElement("div");
    group.className = "af-voice-group";

    const row = document.createElement("div");
    row.className = "af-voice-row";

    const name = document.createElement("span");
    name.className = "af-voice-term";
    name.textContent = "Voice";

    const voices = tts.voices;
    const select = voiceOptions(voices, ttsVoice());

    const current = ttsVoice();
    select.addEventListener("change", () => {
        const wanted = select.value;
        select.disabled = true;
        void chooseBranch("voice", wanted, apiBase).then(ok => {
            select.disabled = false;
            onChanged();
            if (!ok) {
                // Back to what is in force, not to blank: a refused change has
                // not altered anything, and blank would claim it had.
                select.value = current;
            }
        });
    });

    row.append(name, select);
    group.append(
        row,
        detailLine(
            voices.length
                ? "What this deployment speaks in, including when it speaks into a room."
                : "The synthesiser this deployment names chooses its own voice.",
        ),
    );
    return group;
}

/** Whether this machine's microphone works, which nothing else here answers. */
function buildCheckButton(apiBase: string): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.className = "af-settings-btn";
    btn.textContent = "Test microphone";
    btn.title = "Record one turn and show what was heard. Nothing is sent to the chat.";
    btn.addEventListener("click", () => void checkMicrophone(btn, apiBase));
    return btn;
}

/** Hand every branch back to the environment this deployment was configured with. */
async function resetToConfigured(btn: HTMLButtonElement, apiBase: string): Promise<void> {
    btn.disabled = true;
    try {
        const res = await fetch(`${apiBase}/api/voice`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reset: true }),
        });
        if (!res.ok) {
            const body = (await res.json()) as { error?: string };
            toast.show({ type: "alert-error", title: "Could not reset", message: body.error ?? "" });
        }
    } catch {
        toast.show({ type: "alert-error", title: "Could not reset", message: "The server did not answer." });
    } finally {
        btn.disabled = false;
    }
}

/** The way back from a choice, since choices are kept. */
function buildResetButton(apiBase: string, onChanged: () => void): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.className = "af-settings-btn";
    btn.textContent = "Reset to configured";
    btn.title = "Drop the choices made here and use what the machine is configured with";
    btn.addEventListener("click", () => void resetToConfigured(btn, apiBase).then(onChanged));
    return btn;
}

/** The controls that go under the section: the way back, and `host`'s only one. */
function buildButtons(apiBase: string, onChanged: () => void): HTMLElement {
    const buttons = document.createElement("div");
    buttons.className = "af-voice-buttons";
    if (sttMode() === "host") {
        buttons.appendChild(buildCheckButton(apiBase));
    }
    buttons.appendChild(buildResetButton(apiBase, onChanged));
    return buttons;
}

/** The voice section, or nothing when this deployment neither listens nor speaks. */
export function buildVoiceSection(apiBase: string, onChanged: () => void = () => {}): HTMLElement | null {
    if (sttMode() === "off" && ttsMode() === "off") {
        // A section saying twice that nothing happens is worse than no section.
        return null;
    }

    const section = document.createElement("div");
    section.className = "af-settings-section";

    const heading = document.createElement("h3");
    heading.className = "af-settings-section-heading";
    heading.textContent = "Voice";
    section.appendChild(heading);

    section.appendChild(
        buildBranchPicker(
            "Listening",
            "listening",
            sttMode(),
            STT_BRANCHES,
            listeningSays(),
            apiBase,
            onChanged,
        ),
    );
    section.appendChild(
        buildBranchPicker(
            "Speaking",
            "speaking",
            ttsMode(),
            TTS_BRANCHES,
            speakingSays(),
            apiBase,
            onChanged,
        ),
    );

    const note = document.createElement("p");
    note.className = "af-settings-note";
    note.textContent = "Changed here, and kept until reset. Where the services live is set on the machine.";
    section.appendChild(note);

    if (ttsMode() !== "off") {
        section.appendChild(buildVoicePicker(apiBase, onChanged));
    }
    section.appendChild(buildButtons(apiBase, onChanged));
    return section;
}
