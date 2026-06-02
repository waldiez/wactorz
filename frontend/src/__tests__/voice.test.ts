import { describe, it, expect, vi, beforeEach } from "vitest";
import { VoiceInput } from "../io/VoiceInput";

// ── Minimal SpeechRecognition mock ────────────────────────────────────────────

class MockSpeechRecognition {
  continuous = false;
  interimResults = false;
  lang = "";
  onresult: ((e: unknown) => void) | null = null;
  onend: (() => void) | null = null;
  onerror: ((e: { error: string }) => void) | null = null;
  start = vi.fn();
  stop = vi.fn();
}

let mockInstance: MockSpeechRecognition | null = null;
const allMockInstances: MockSpeechRecognition[] = [];

function installMock() {
  allMockInstances.length = 0;
  (globalThis as any).SpeechRecognition = class extends MockSpeechRecognition {
    constructor() { super(); mockInstance = this; allMockInstances.push(this); }
  };
  delete (globalThis as any).webkitSpeechRecognition;
}

function removeMock() {
  delete (globalThis as any).SpeechRecognition;
  delete (globalThis as any).webkitSpeechRecognition;
}

// Helper: make navigator.mediaDevices.getUserMedia resolve successfully.
function allowMic() {
  Object.defineProperty(globalThis, "navigator", {
    value: {
      mediaDevices: {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [{ stop: vi.fn() }],
        }),
      },
    },
    configurable: true,
    writable: true,
  });
}

// Helper: make getUserMedia reject (mic denied).
function denyMic() {
  Object.defineProperty(globalThis, "navigator", {
    value: {
      mediaDevices: {
        getUserMedia: vi.fn().mockRejectedValue(new Error("NotAllowedError")),
      },
    },
    configurable: true,
    writable: true,
  });
}

beforeEach(() => {
  mockInstance = null;
  vi.clearAllMocks();
});

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("VoiceInput — API unavailable", () => {
  it("isAvailable returns false when SpeechRecognition is not in globalThis", () => {
    removeMock();
    const v = new VoiceInput();
    expect(v.isAvailable).toBe(false);
    expect(v.isRecording).toBe(false);
  });

  it("start() returns false when API is unavailable", async () => {
    removeMock();
    const v = new VoiceInput();
    const result = await v.start();
    expect(result).toBe(false);
  });

  it("uses webkitSpeechRecognition as fallback", () => {
    removeMock();
    (globalThis as any).webkitSpeechRecognition = class extends MockSpeechRecognition {
      constructor() { super(); mockInstance = this; }
    };
    const v = new VoiceInput();
    expect(v.isAvailable).toBe(true);
    delete (globalThis as any).webkitSpeechRecognition;
  });
});

describe("VoiceInput — API available", () => {
  beforeEach(() => { installMock(); allowMic(); });
  afterEach(() => { removeMock(); });

  it("isAvailable returns true", () => {
    const v = new VoiceInput();
    expect(v.isAvailable).toBe(true);
  });

  it("configures recognition with correct settings", () => {
    new VoiceInput();
    expect(mockInstance!.continuous).toBe(false);
    expect(mockInstance!.interimResults).toBe(true);
    expect(mockInstance!.lang).toBe("en-US");
  });

  // ── start ──────────────────────────────────────────────────────────────────

  it("start() calls recognition.start() and returns true", async () => {
    const v = new VoiceInput();
    const ok = await v.start();
    expect(ok).toBe(true);
    expect(mockInstance!.start).toHaveBeenCalledOnce();
    expect(v.isRecording).toBe(true);
  });

  it("start() returns false if already recording", async () => {
    const v = new VoiceInput();
    await v.start();
    const ok = await v.start();
    expect(ok).toBe(false);
    expect(mockInstance!.start).toHaveBeenCalledOnce();
  });

  it("start() returns false and calls onError when mic denied", async () => {
    denyMic();
    const v = new VoiceInput();
    const errSpy = vi.fn();
    v.onError = errSpy;
    const ok = await v.start();
    expect(ok).toBe(false);
    expect(errSpy).toHaveBeenCalledWith(expect.stringContaining("denied"));
  });

  // ── stop ───────────────────────────────────────────────────────────────────

  it("stop() calls recognition.stop() when recording", async () => {
    const v = new VoiceInput();
    await v.start();
    v.stop();
    expect(mockInstance!.stop).toHaveBeenCalledOnce();
    expect(v.isRecording).toBe(false);
  });

  it("stop() is a no-op when not recording", () => {
    const v = new VoiceInput();
    expect(() => v.stop()).not.toThrow();
    expect(mockInstance!.stop).not.toHaveBeenCalled();
  });

  // ── onresult ───────────────────────────────────────────────────────────────

  it("fires onTranscript with final=true on final result", async () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onTranscript = spy;
    await v.start();

    const evt = {
      resultIndex: 0,
      results: {
        length: 1,
        0: { isFinal: true, length: 1, 0: { transcript: "hello world" } },
      },
    };
    mockInstance!.onresult!(evt);
    expect(spy).toHaveBeenCalledWith("hello world", true);
  });

  it("fires onTranscript with final=false on interim result", async () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onTranscript = spy;
    await v.start();

    const evt = {
      resultIndex: 0,
      results: {
        length: 1,
        0: { isFinal: false, length: 1, 0: { transcript: "hel" } },
      },
    };
    mockInstance!.onresult!(evt);
    expect(spy).toHaveBeenCalledWith("hel", false);
  });

  it("handles result without transcript gracefully", async () => {
    const v = new VoiceInput();
    v.onTranscript = vi.fn();
    await v.start();
    const evt = {
      resultIndex: 0,
      results: {
        length: 1,
        0: { isFinal: false, length: 1, 0: undefined },
      },
    };
    expect(() => mockInstance!.onresult!(evt)).not.toThrow();
  });

  it("onresult skips null result entry (covers !result continue branch)", async () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onTranscript = spy;
    await v.start();
    const evt = {
      resultIndex: 0,
      results: { length: 1, 0: null },
    };
    expect(() => mockInstance!.onresult!(evt)).not.toThrow();
    expect(spy).not.toHaveBeenCalled();
  });

  // ── onend ──────────────────────────────────────────────────────────────────

  it("onend clears isRecording and calls onStop", async () => {
    const v = new VoiceInput();
    const stopSpy = vi.fn();
    v.onStop = stopSpy;
    await v.start();
    mockInstance!.onend!();
    expect(v.isRecording).toBe(false);
    expect(stopSpy).toHaveBeenCalledOnce();
  });

  // ── onerror ────────────────────────────────────────────────────────────────

  it("onerror calls onError with user message for 'not-allowed'", async () => {
    const v = new VoiceInput();
    const errSpy = vi.fn();
    v.onError = errSpy;
    await v.start();
    mockInstance!.onerror!({ error: "not-allowed" });
    expect(errSpy).toHaveBeenCalledWith(expect.stringContaining("denied"));
  });

  it("onerror calls onError for 'service-not-allowed'", async () => {
    const v = new VoiceInput();
    const errSpy = vi.fn();
    v.onError = errSpy;
    await v.start();
    mockInstance!.onerror!({ error: "service-not-allowed" });
    expect(errSpy).toHaveBeenCalledWith(expect.stringContaining("HTTPS"));
  });

  it("onerror calls onError for 'audio-capture'", async () => {
    const v = new VoiceInput();
    const errSpy = vi.fn();
    v.onError = errSpy;
    await v.start();
    mockInstance!.onerror!({ error: "audio-capture" });
    expect(errSpy).toHaveBeenCalledWith(expect.stringContaining("microphone"));
  });

  it("onerror nulls recognition for permanent errors (making isAvailable false)", async () => {
    const v = new VoiceInput();
    await v.start();
    mockInstance!.onerror!({ error: "not-allowed" });
    expect(v.isAvailable).toBe(false);
  });

  it("onerror does not call onError for 'no-speech'", async () => {
    const v = new VoiceInput();
    const errSpy = vi.fn();
    v.onError = errSpy;
    await v.start();
    mockInstance!.onerror!({ error: "no-speech" });
    expect(errSpy).not.toHaveBeenCalled();
  });

  it("onerror does not call onError for 'aborted'", async () => {
    const v = new VoiceInput();
    const errSpy = vi.fn();
    v.onError = errSpy;
    await v.start();
    mockInstance!.onerror!({ error: "aborted" });
    expect(errSpy).not.toHaveBeenCalled();
  });

  it("onerror calls onError for 'network' error", async () => {
    const v = new VoiceInput();
    const errSpy = vi.fn();
    v.onError = errSpy;
    await v.start();
    mockInstance!.onerror!({ error: "network" });
    expect(errSpy).toHaveBeenCalledWith(expect.stringContaining("Network"));
  });

  it("onerror calls onStop after any error", async () => {
    const v = new VoiceInput();
    const stopSpy = vi.fn();
    v.onStop = stopSpy;
    await v.start();
    mockInstance!.onerror!({ error: "no-speech" });
    expect(stopSpy).toHaveBeenCalledOnce();
  });

  it("onerror logs warning for unknown error type (not in message map, not no-speech/aborted)", async () => {
    const v = new VoiceInput();
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    await v.start();
    mockInstance!.onerror!({ error: "some-unknown-error" });
    expect(warnSpy).toHaveBeenCalledWith("[VoiceInput] Recognition error:", "some-unknown-error");
    warnSpy.mockRestore();
  });

  // ── onend with ambient ────────────────────────────────────────────────────

  it("onend schedules ambient restart when _isAmbient is true", async () => {
    vi.useFakeTimers();
    const v = new VoiceInput();
    v.startAmbient("hey"); // sets _isAmbient=true, launches ambient (allMockInstances[1])
    await v.start();       // pauses ambient; PTT rec = allMockInstances[0]
    // Call PTT rec's onend (not ambient's) to exercise the _isAmbient branch on line 122
    allMockInstances[0]!.onend!();
    vi.advanceTimersByTime(400); // let the 300ms _scheduleAmbient timer fire
    // A new ambient session should have been launched (3rd instance)
    expect(allMockInstances.length).toBeGreaterThanOrEqual(3);
    vi.useRealTimers();
  });

  // ── start() with ambient active ───────────────────────────────────────────

  it("start() when _isAmbient suspends ambient session", async () => {
    const v = new VoiceInput();
    v.startAmbient("hey"); // ambient session created
    expect(allMockInstances.length).toBe(2); // PTT rec + ambient rec
    const ambientRec = allMockInstances[1]!;
    await v.start();
    expect(ambientRec.stop).toHaveBeenCalled();
  });

  it("start() when mic denied and _isAmbient=true reschedules ambient", async () => {
    vi.useFakeTimers();
    denyMic();
    const v = new VoiceInput();
    v.startAmbient("hey");
    const countBefore = allMockInstances.length;
    await v.start(); // mic denied → should reschedule ambient
    vi.advanceTimersByTime(400);
    expect(allMockInstances.length).toBeGreaterThan(countBefore);
    vi.useRealTimers();
  });
});

// ── VoiceInput — ambient / wake-word mode ─────────────────────────────────────

describe("VoiceInput — ambient mode", () => {
  beforeEach(() => { installMock(); allowMic(); allMockInstances.length = 0; });
  afterEach(() => { removeMock(); });

  it("startAmbient() returns false when API is unavailable", () => {
    removeMock();
    const v = new VoiceInput();
    expect(v.startAmbient("hey")).toBe(false);
    expect(v.isAmbient).toBe(false);
  });

  it("startAmbient() returns false when already in ambient mode", () => {
    const v = new VoiceInput();
    v.startAmbient("hey");
    expect(v.startAmbient("hey")).toBe(false);
  });

  it("startAmbient() sets isAmbient=true and launches ambient session", () => {
    const v = new VoiceInput();
    const ok = v.startAmbient("computer");
    expect(ok).toBe(true);
    expect(v.isAmbient).toBe(true);
    // Two instances: one PTT, one ambient
    expect(allMockInstances.length).toBe(2);
  });

  it("startAmbient() normalises wakeWord to lowercase", () => {
    const v = new VoiceInput();
    v.startAmbient("HEY COMPUTER");
    // Just verify it doesn't throw and isAmbient is true
    expect(v.isAmbient).toBe(true);
  });

  it("stopAmbient() clears isAmbient and stops ambient session", () => {
    const v = new VoiceInput();
    v.startAmbient("hey");
    const ambRec = allMockInstances[1]!;
    v.stopAmbient();
    expect(v.isAmbient).toBe(false);
    expect(ambRec.stop).toHaveBeenCalled();
  });

  it("stopAmbient() is safe when not in ambient mode", () => {
    const v = new VoiceInput();
    expect(() => v.stopAmbient()).not.toThrow();
  });

  it("stopAmbient() cancels pending _ambientTimer", () => {
    vi.useFakeTimers();
    const v = new VoiceInput();
    v.startAmbient("hey");
    const ambRec = allMockInstances[1]!;
    ambRec.onend!(); // fires → schedules 300ms timer
    // Timer is now pending; call stopAmbient before it fires
    v.stopAmbient(); // should clear the timer
    vi.advanceTimersByTime(400);
    // No new ambient instance should have been created (timer was cleared)
    expect(allMockInstances.length).toBe(2);
    vi.useRealTimers();
  });

  // ── ambient onresult / wake word detection ────────────────────────────────

  it("ambient onresult fires onWakeWord when wake word found", () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onWakeWord = spy;
    v.startAmbient("computer");
    const ambRec = allMockInstances[1]!;
    const evt = {
      resultIndex: 0,
      results: {
        length: 1,
        0: { isFinal: true, length: 1, 0: { transcript: "hey computer open settings" } },
      },
    };
    ambRec.onresult!(evt);
    expect(spy).toHaveBeenCalledWith("open settings");
  });

  it("ambient onresult fires onWakeWord with empty string when no text after keyword", () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onWakeWord = spy;
    v.startAmbient("computer");
    const ambRec = allMockInstances[1]!;
    const evt = {
      resultIndex: 0,
      results: { length: 1, 0: { isFinal: true, length: 1, 0: { transcript: "computer" } } },
    };
    ambRec.onresult!(evt);
    expect(spy).toHaveBeenCalledWith("");
  });

  it("ambient onresult ignores non-final results", () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onWakeWord = spy;
    v.startAmbient("computer");
    const ambRec = allMockInstances[1]!;
    const evt = {
      resultIndex: 0,
      results: { length: 1, 0: { isFinal: false, length: 1, 0: { transcript: "computer" } } },
    };
    ambRec.onresult!(evt);
    expect(spy).not.toHaveBeenCalled();
  });

  it("ambient onresult ignores results without wake word", () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onWakeWord = spy;
    v.startAmbient("computer");
    const ambRec = allMockInstances[1]!;
    const evt = {
      resultIndex: 0,
      results: { length: 1, 0: { isFinal: true, length: 1, 0: { transcript: "hello there" } } },
    };
    ambRec.onresult!(evt);
    expect(spy).not.toHaveBeenCalled();
  });

  it("ambient onresult handles undefined result[0] gracefully (covers ?? '' branch)", () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onWakeWord = spy;
    v.startAmbient("computer");
    const ambRec = allMockInstances[1]!;
    // result[0] is undefined → result[0]?.transcript is undefined → ?? "" gives ""
    const evt = {
      resultIndex: 0,
      results: { length: 1, 0: { isFinal: true, length: 0, 0: undefined } },
    };
    expect(() => ambRec.onresult!(evt)).not.toThrow();
    expect(spy).not.toHaveBeenCalled();
  });

  // ── ambient onend (restart) ───────────────────────────────────────────────

  it("ambient onend schedules a new ambient session after 300ms", () => {
    vi.useFakeTimers();
    const v = new VoiceInput();
    v.startAmbient("hey");
    const ambRec = allMockInstances[1]!;
    ambRec.onend!(); // session ended → should schedule restart
    expect(allMockInstances.length).toBe(2); // no new instance yet
    vi.advanceTimersByTime(350);
    expect(allMockInstances.length).toBe(3); // new ambient session started
    vi.useRealTimers();
  });

  it("ambient onend does not restart when stopAmbient was called", () => {
    vi.useFakeTimers();
    const v = new VoiceInput();
    v.startAmbient("hey");
    const ambRec = allMockInstances[1]!;
    v.stopAmbient(); // sets _isAmbient=false
    ambRec.onend!(); // ended, but isAmbient=false → no restart
    vi.advanceTimersByTime(500);
    expect(allMockInstances.length).toBe(2); // no new instance
    vi.useRealTimers();
  });

  // ── ambient onerror ───────────────────────────────────────────────────────

  it("ambient onerror for permanent error calls onAmbientStop and clears isAmbient", () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onAmbientStop = spy;
    v.startAmbient("hey");
    const ambRec = allMockInstances[1]!;
    ambRec.onerror!({ error: "not-allowed" });
    expect(spy).toHaveBeenCalled();
    expect(v.isAmbient).toBe(false);
  });

  it("ambient onerror for 'service-not-allowed' calls onAmbientStop", () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onAmbientStop = spy;
    v.startAmbient("hey");
    allMockInstances[1]!.onerror!({ error: "service-not-allowed" });
    expect(spy).toHaveBeenCalled();
  });

  it("ambient onerror for 'audio-capture' calls onAmbientStop", () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onAmbientStop = spy;
    v.startAmbient("hey");
    allMockInstances[1]!.onerror!({ error: "audio-capture" });
    expect(spy).toHaveBeenCalled();
  });

  it("ambient onerror for non-permanent error does not call onAmbientStop", () => {
    const v = new VoiceInput();
    const spy = vi.fn();
    v.onAmbientStop = spy;
    v.startAmbient("hey");
    allMockInstances[1]!.onerror!({ error: "network" });
    expect(spy).not.toHaveBeenCalled();
    expect(v.isAmbient).toBe(true);
  });

  // ── _launchAmbient throws ─────────────────────────────────────────────────

  it("_launchAmbient() schedules retry when rec.start() throws", () => {
    vi.useFakeTimers();
    let throwCount = 0;
    (globalThis as any).SpeechRecognition = class extends MockSpeechRecognition {
      constructor() {
        super();
        allMockInstances.push(this);
        mockInstance = this;
        // Second instance is the first ambient rec — make its start throw
        if (allMockInstances.length === 2 && throwCount === 0) {
          throwCount++;
          this.start = vi.fn().mockImplementation(() => { throw new Error("already started"); });
        }
      }
    };
    const v = new VoiceInput(); // instance 1 (PTT)
    v.startAmbient("hey");     // instance 2 (ambient, start throws) → schedules retry
    vi.advanceTimersByTime(400); // retry fires → instance 3 created
    expect(allMockInstances.length).toBeGreaterThanOrEqual(3);
    vi.useRealTimers();
  });
});
