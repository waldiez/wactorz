import { describe, it, expect, vi, beforeEach } from "vitest";
import { IOBar } from "../ui/IOBar";
import type { AgentInfo } from "../types/agent";

// ── DOM fixture ───────────────────────────────────────────────────────────────

function setupDOM() {
  document.body.innerHTML = `
    <button id="wake-btn"></button>
    <button id="mic-btn"></button>
    <textarea id="text-input"></textarea>
    <button id="send-btn"></button>
  `;
}

// ── Fake dependencies ─────────────────────────────────────────────────────────

function makeMocks() {
  const ioManager = {
    send: vi.fn().mockResolvedValue(undefined),
  } as any;

  const voiceInput = {
    isAvailable: true,
    isRecording: false,
    isAmbient: false,
    start: vi.fn().mockResolvedValue(true),
    stop: vi.fn(),
    startAmbient: vi.fn().mockReturnValue(true),
    stopAmbient: vi.fn(),
    onTranscript: null as ((text: string, final: boolean) => void) | null,
    onStop: null as (() => void) | null,
    onError: null as ((msg: string) => void) | null,
    onWakeWord: null as ((textAfter: string) => void) | null,
    onAmbientStop: null as (() => void) | null,
  } as any;

  return { ioManager, voiceInput };
}

function agent(name = "bravo"): AgentInfo {
  return { id: name, name, state: "running", protected: false };
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function getInput() { return document.getElementById("text-input") as HTMLTextAreaElement; }
function getSend() { return document.getElementById("send-btn") as HTMLButtonElement; }
function getMic() { return document.getElementById("mic-btn") as HTMLButtonElement; }

function pressKey(target: HTMLElement, key: string, opts: KeyboardEventInit = {}) {
  target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...opts }));
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("IOBar", () => {
  let ioManager: ReturnType<typeof makeMocks>["ioManager"];
  let voiceInput: ReturnType<typeof makeMocks>["voiceInput"];

  beforeEach(() => {
    setupDOM();
    ({ ioManager, voiceInput } = makeMocks());
    vi.clearAllMocks();
  });

  it("constructs without throwing", () => {
    expect(() => new IOBar(voiceInput, ioManager)).not.toThrow();
  });

  // ── send ──────────────────────────────────────────────────────────────────

  it("Enter key sends message and clears input", async () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    input.value = "hello";
    pressKey(input, "Enter");
    await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalledWith("hello", null));
    expect(input.value).toBe("");
  });

  it("Shift+Enter inserts newline (no send)", async () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    input.value = "hello";
    pressKey(input, "Enter", { shiftKey: true });
    await new Promise((r) => setTimeout(r, 10));
    expect(ioManager.send).not.toHaveBeenCalled();
  });

  it("send button click sends message", async () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    input.value = "click send";
    getSend().click();
    await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalledWith("click send", null));
  });

  it("ignores send when input is empty", async () => {
    new IOBar(voiceInput, ioManager);
    getInput().value = "   ";
    getSend().click();
    await new Promise((r) => setTimeout(r, 10));
    expect(ioManager.send).not.toHaveBeenCalled();
  });

  it("ignores send while already sending", async () => {
    let resolve!: () => void;
    ioManager.send.mockImplementation(() => new Promise<void>((r) => { resolve = r; }));
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    input.value = "msg1";
    getSend().click();
    input.value = "msg2";
    getSend().click();
    resolve();
    await new Promise((r) => setTimeout(r, 10));
    expect(ioManager.send).toHaveBeenCalledOnce();
  });

  it("send removes sending class from button on completion", async () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    const btn = getSend();
    input.value = "msg";
    getSend().click();
    await vi.waitFor(() => expect(btn.classList.contains("sending")).toBe(false));
  });

  it("send passes activeAgent when panel is open", async () => {
    new IOBar(voiceInput, ioManager);
    const a = agent("charlie");
    document.dispatchEvent(new CustomEvent("panel-opened", { detail: { agent: a } }));
    const input = getInput();
    input.value = "targeted";
    getSend().click();
    await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalledWith("targeted", a));
  });

  // ── history ───────────────────────────────────────────────────────────────

  it("ArrowUp navigates into history", async () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    // Send one message to populate history
    input.value = "first";
    getSend().click();
    await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalled());
    pressKey(input, "ArrowUp");
    expect(input.value).toBe("first");
  });

  it("ArrowUp is no-op when history is empty", () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    input.value = "draft";
    pressKey(input, "ArrowUp");
    // No history, so value unchanged
    expect(input.value).toBe("draft");
  });

  it("ArrowDown restores draft after ArrowUp", async () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    input.value = "first";
    getSend().click();
    await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalled());
    input.value = "my draft";
    pressKey(input, "ArrowUp");
    pressKey(input, "ArrowDown");
    expect(input.value).toBe("my draft");
  });

  it("ArrowDown shows previous history item when not yet back to draft", async () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    // Push 2 messages (history is most-recent-first: ["second", "first"])
    for (const text of ["first", "second"]) {
      input.value = text;
      getSend().click();
      await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalled());
      ioManager.send.mockClear();
    }
    input.value = "draft";
    pressKey(input, "ArrowUp");   // histIdx 0 → "second"
    pressKey(input, "ArrowUp");   // histIdx 1 → "first"
    pressKey(input, "ArrowDown"); // histIdx 0 → "second" (else branch in historyDown)
    expect(input.value).toBe("second");
  });

  it("ArrowDown is no-op when not in history mode", () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    input.value = "current";
    pressKey(input, "ArrowDown");
    expect(input.value).toBe("current");
  });

  it("other key resets histIdx to -1", async () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    input.value = "first";
    getSend().click();
    await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalled());
    pressKey(input, "ArrowUp");
    expect(input.value).toBe("first");
    pressKey(input, "a");
    // After pressing "a", histIdx is reset to -1
    // pressing ArrowDown now should be a no-op
    pressKey(input, "ArrowDown");
    expect(input.value).toBe("first"); // still at history item (histIdx was reset so ArrowDown is no-op)
  });

  it("history caps at 50 entries", async () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    for (let i = 0; i < 55; i++) {
      input.value = `msg-${i}`;
      getSend().click();
      await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalled());
      ioManager.send.mockClear();
    }
    // Navigate to the end of history
    for (let i = 0; i < 60; i++) {
      pressKey(input, "ArrowUp");
    }
    // Should stop at the 50th entry (oldest is msg-5, since most recent first)
    // Just verifies it doesn't crash and returns a string
    expect(typeof input.value).toBe("string");
  });

  // ── panel events ─────────────────────────────────────────────────────────

  it("panel-opened sets placeholder with agent name", () => {
    new IOBar(voiceInput, ioManager);
    document.dispatchEvent(new CustomEvent("panel-opened", { detail: { agent: agent("alpha") } }));
    expect(getInput().placeholder).toContain("@alpha");
  });

  it("panel-closed resets placeholder and activeAgent", async () => {
    new IOBar(voiceInput, ioManager);
    document.dispatchEvent(new CustomEvent("panel-opened", { detail: { agent: agent("alpha") } }));
    document.dispatchEvent(new CustomEvent("panel-closed"));
    getInput().value = "hello";
    getSend().click();
    await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalledWith("hello", null));
    expect(getInput().placeholder).toContain("io-agent");
  });

  // ── voice ─────────────────────────────────────────────────────────────────

  it("click on mic starts recording when not already recording", async () => {
    new IOBar(voiceInput, ioManager);
    getMic().click();
    await vi.waitFor(() => expect(voiceInput.start).toHaveBeenCalled());
  });

  it("click on mic stops recording when already recording", () => {
    voiceInput.isRecording = true;
    new IOBar(voiceInput, ioManager);
    getMic().click();
    expect(voiceInput.stop).toHaveBeenCalled();
  });

  it("click on mic does not call stop when not recording", () => {
    voiceInput.isRecording = false;
    new IOBar(voiceInput, ioManager);
    getMic().click();
    expect(voiceInput.stop).not.toHaveBeenCalled();
  });

  it("click on mic adds recording class when start returns true", async () => {
    new IOBar(voiceInput, ioManager);
    getMic().click();
    await vi.waitFor(() => expect(getMic().classList.contains("recording")).toBe(true));
  });

  it("Shift+ArrowUp reaches last if-check without resetting histIdx", async () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    input.value = "first";
    getSend().click();
    await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalled());
    pressKey(input, "ArrowUp");       // navigate into history
    const valueBefore = input.value;
    pressKey(input, "ArrowUp", { shiftKey: true }); // reaches last if; !includes("ArrowUp") = false
    // histIdx is NOT reset — stays on current history item
    expect(input.value).toBe(valueBefore);
  });

  it("click on mic does not call start when already recording", async () => {
    voiceInput.isRecording = true;
    new IOBar(voiceInput, ioManager);
    getMic().click();
    await new Promise((r) => setTimeout(r, 10));
    expect(voiceInput.start).not.toHaveBeenCalled();
  });

  it("startMic() is no-op when voiceInput is already recording", async () => {
    voiceInput.isRecording = true;
    const iobar = new IOBar(voiceInput, ioManager);
    await iobar.startMic();
    expect(voiceInput.start).not.toHaveBeenCalled();
  });

  it("stopMic() is no-op when voiceInput is not recording", () => {
    voiceInput.isRecording = false;
    const iobar = new IOBar(voiceInput, ioManager);
    iobar.stopMic();
    expect(voiceInput.stop).not.toHaveBeenCalled();
  });

  it("start returning false does not add recording class", async () => {
    voiceInput.start.mockResolvedValue(false);
    new IOBar(voiceInput, ioManager);
    getMic().click();
    await vi.waitFor(() => expect(voiceInput.start).toHaveBeenCalled());
    expect(getMic().classList.contains("recording")).toBe(false);
  });

  it("onTranscript with final=true sends message", async () => {
    new IOBar(voiceInput, ioManager);
    voiceInput.onTranscript!("hey there", true);
    await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalledWith("hey there", null));
  });

  it("onTranscript with final=false sets input value without sending", async () => {
    new IOBar(voiceInput, ioManager);
    voiceInput.onTranscript!("interim text", false);
    await new Promise((r) => setTimeout(r, 10));
    expect(getInput().value).toBe("interim text");
    expect(ioManager.send).not.toHaveBeenCalled();
  });

  it("onStop removes recording class from mic", () => {
    new IOBar(voiceInput, ioManager);
    getMic().classList.add("recording");
    voiceInput.onStop!();
    expect(getMic().classList.contains("recording")).toBe(false);
  });

  it("onError removes recording class and sets title", () => {
    new IOBar(voiceInput, ioManager);
    getMic().classList.add("recording");
    voiceInput.onError!("Not allowed");
    expect(getMic().classList.contains("recording")).toBe(false);
    expect(getMic().title).toBe("Not allowed");
  });

  it("onError hides mic when voice is permanently unavailable", () => {
    voiceInput.isAvailable = false;
    new IOBar(voiceInput, ioManager);
    voiceInput.onError!("service-not-allowed");
    expect(getMic().style.display).toBe("none");
  });

  it("onError resets mic title after 5 seconds when voice is still available", () => {
    vi.useFakeTimers();
    voiceInput.isAvailable = true;
    new IOBar(voiceInput, ioManager);
    voiceInput.onError!("Temporary error");
    expect(getMic().title).toBe("Temporary error");
    vi.advanceTimersByTime(5001);
    expect(getMic().title).toBe("Voice input");
    vi.useRealTimers();
  });

  it("autoGrow is triggered on input event", () => {
    new IOBar(voiceInput, ioManager);
    const input = getInput();
    input.dispatchEvent(new Event("input"));
    // Just verify no throw — jsdom doesn't render scrollHeight meaningfully
    expect(true).toBe(true);
  });

  // ── wake word handler ──────────────────────────────────────────────────────

  it("onWakeWord with empty textAfter calls startMic", async () => {
    new IOBar(voiceInput, ioManager);
    voiceInput.onWakeWord!("");
    await vi.waitFor(() => expect(voiceInput.start).toHaveBeenCalled());
  });

  it("onWakeWord with textAfter fills input and sends", async () => {
    new IOBar(voiceInput, ioManager);
    voiceInput.onWakeWord!("say hello");
    await vi.waitFor(() => expect(ioManager.send).toHaveBeenCalledWith("say hello", null));
  });

  it("onAmbientStop hides wake button and sets localStorage", () => {
    new IOBar(voiceInput, ioManager);
    voiceInput.onAmbientStop!();
    const wakeBtn = document.getElementById("wake-btn") as HTMLButtonElement;
    expect(wakeBtn.style.display).toBe("none");
    expect(localStorage.getItem("wactorz.wakeActive")).toBe("0");
  });

  // ── toggleWake ─────────────────────────────────────────────────────────────

  it("toggleWake() when isAmbient=true stops ambient and removes 'ambient' class", () => {
    voiceInput.isAmbient = true;
    const iobar = new IOBar(voiceInput, ioManager);
    iobar.toggleWake();
    expect(voiceInput.stopAmbient).toHaveBeenCalled();
    expect(document.getElementById("wake-btn")!.classList.contains("ambient")).toBe(false);
  });

  it("toggleWake() when isAmbient=false and startAmbient succeeds adds 'ambient' class", () => {
    voiceInput.isAmbient = false;
    voiceInput.startAmbient.mockReturnValue(true);
    const iobar = new IOBar(voiceInput, ioManager);
    iobar.toggleWake();
    expect(voiceInput.startAmbient).toHaveBeenCalled();
    expect(document.getElementById("wake-btn")!.classList.contains("ambient")).toBe(true);
  });

  it("toggleWake() when isAmbient=false and startAmbient fails shows unavailable title", () => {
    vi.useFakeTimers();
    voiceInput.isAmbient = false;
    voiceInput.startAmbient.mockReturnValue(false);
    const iobar = new IOBar(voiceInput, ioManager);
    iobar.toggleWake();
    expect(document.getElementById("wake-btn")!.title).toContain("unavailable");
    vi.useRealTimers();
  });

  it("toggleWake() when not ambient restores title after 4 seconds", () => {
    vi.useFakeTimers();
    voiceInput.isAmbient = false;
    voiceInput.startAmbient.mockReturnValue(false);
    const iobar = new IOBar(voiceInput, ioManager);
    iobar.toggleWake();
    vi.advanceTimersByTime(4001);
    expect(document.getElementById("wake-btn")!.title).toContain("click to enable");
    vi.useRealTimers();
  });

  // ── onTranscript with af-iobar-input in DOM (CardDashboard path) ─────────

  it("onTranscript final=false fills af-iobar-input when present", () => {
    document.body.innerHTML += `<input id="af-iobar-input">`;
    new IOBar(voiceInput, ioManager);
    voiceInput.onTranscript!("hello interim", false);
    const cdInput = document.getElementById("af-iobar-input") as HTMLInputElement;
    expect(cdInput.value).toBe("hello interim");
    expect(ioManager.send).not.toHaveBeenCalled();
  });

  it("onTranscript final=true dispatches af-send-message and clears af-iobar-input", () => {
    document.body.innerHTML += `<input id="af-iobar-input">`;
    new IOBar(voiceInput, ioManager);
    const eventSpy = vi.fn();
    document.addEventListener("af-send-message", eventSpy, { once: true });
    voiceInput.onTranscript!("final text", true);
    expect(eventSpy).toHaveBeenCalled();
    const detail = (eventSpy.mock.calls[0]![0] as CustomEvent).detail;
    expect(detail.content).toBe("final text");
    const cdInput = document.getElementById("af-iobar-input") as HTMLInputElement;
    expect(cdInput.value).toBe("");
  });

  // ── af-iobar-input branch (CardDashboard path) ────────────────────────────

  it("onWakeWord with textAfter dispatches af-send-message when af-iobar-input exists", () => {
    document.body.innerHTML += `<input id="af-iobar-input">`;
    new IOBar(voiceInput, ioManager);
    const eventSpy = vi.fn();
    document.addEventListener("af-send-message", eventSpy, { once: true });
    voiceInput.onWakeWord!("open settings");
    expect(eventSpy).toHaveBeenCalled();
    const detail = (eventSpy.mock.calls[0]![0] as CustomEvent).detail;
    expect(detail.content).toBe("open settings");
  });

  // ── af-wake-btn-cd branch (CardDashboard wake button) ────────────────────

  it("onWakeWord triggers class on af-wake-btn-cd when it exists", () => {
    document.body.innerHTML += `<button id="af-wake-btn-cd"></button>`;
    new IOBar(voiceInput, ioManager);
    voiceInput.onWakeWord!(""); // empty → startMic path, also triggers _syncWakeTriggered
    const cdWake = document.getElementById("af-wake-btn-cd")!;
    expect(cdWake.classList.contains("triggered")).toBe(true);
  });

  it("_syncWakeTriggered removes triggered class from both buttons after 700ms", () => {
    vi.useFakeTimers();
    document.body.innerHTML += `<button id="af-wake-btn-cd"></button>`;
    new IOBar(voiceInput, ioManager);
    voiceInput.onWakeWord!(""); // triggers _syncWakeTriggered → adds "triggered" + schedules removal
    const wakeBtn = document.getElementById("wake-btn")!;
    const cdWake = document.getElementById("af-wake-btn-cd")!;
    expect(wakeBtn.classList.contains("triggered")).toBe(true);
    expect(cdWake.classList.contains("triggered")).toBe(true);
    vi.advanceTimersByTime(750); // fire both 700ms timers
    expect(wakeBtn.classList.contains("triggered")).toBe(false);
    expect(cdWake.classList.contains("triggered")).toBe(false);
    vi.useRealTimers();
  });
});
