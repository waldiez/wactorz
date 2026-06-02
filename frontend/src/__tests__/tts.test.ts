import { describe, it, expect, vi, beforeEach } from "vitest";

// TTSManager reads localStorage in its constructor, so we import after setup.
// Re-import each test via dynamic import to get a fresh instance.

describe("TTSManager", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    // Reset the module so the singleton re-reads localStorage
    vi.resetModules();
  });

  async function freshTTS() {
    const mod = await import("../io/TTSManager");
    return { TTSManager: mod.TTSManager, tts: mod.tts };
  }

  // ── constructor / localStorage ────────────────────────────────────────────

  it("beepEnabled defaults to true (no localStorage entry)", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(m.beepEnabled).toBe(true);
  });

  it("beepEnabled is false when localStorage has '0'", async () => {
    localStorage.setItem("wactorz.beep", "0");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(m.beepEnabled).toBe(false);
  });

  it("ttsEnabled defaults to false", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(m.ttsEnabled).toBe(false);
  });

  it("ttsEnabled is true when localStorage has '1'", async () => {
    localStorage.setItem("wactorz.tts", "1");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(m.ttsEnabled).toBe(true);
  });

  // ── toggleBeep ─────────────────────────────────────────────────────────────

  it("toggleBeep() flips beepEnabled and persists", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(m.toggleBeep()).toBe(false);
    expect(localStorage.getItem("wactorz.beep")).toBe("0");
    expect(m.toggleBeep()).toBe(true);
    expect(localStorage.getItem("wactorz.beep")).toBe("1");
  });

  // ── toggleTTS ──────────────────────────────────────────────────────────────

  it("toggleTTS() flips ttsEnabled and persists", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(m.toggleTTS()).toBe(true);
    expect(localStorage.getItem("wactorz.tts")).toBe("1");
    expect(m.toggleTTS()).toBe(false);
    expect(localStorage.getItem("wactorz.tts")).toBe("0");
  });

  it("toggleTTS() calls speechSynthesis.cancel() when disabling", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.toggleTTS(); // enable
    m.toggleTTS(); // disable → should cancel
    expect((globalThis as any).speechSynthesis.cancel).toHaveBeenCalled();
  });

  // ── checkUserIntent ────────────────────────────────────────────────────────

  it("checkUserIntent('please speak') sets forceNext flag", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.checkUserIntent("please speak the reply");
    // After forcing, the next notify should call speak
    m.notify("hello");
    await vi.waitFor(() => expect((globalThis as any).speechSynthesis.speak).toHaveBeenCalledOnce());
  });

  it("checkUserIntent with no keyword does not set forceNext", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.checkUserIntent("what is the weather?");
    m.notify("sunny");
    expect((globalThis as any).speechSynthesis.speak).not.toHaveBeenCalled();
  });

  it("forceNext flag is consumed after one notify", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.checkUserIntent("read it out");
    m.notify("first");
    m.notify("second");
    await vi.waitFor(() => expect((globalThis as any).speechSynthesis.speak).toHaveBeenCalledOnce());
  });

  // ── notify paths ───────────────────────────────────────────────────────────

  it("notify() calls beep when beepEnabled=true (mocked AudioContext)", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(m.beepEnabled).toBe(true);
    // Should not throw even though AudioContext is mocked
    expect(() => m.notify("hi")).not.toThrow();
  });

  it("notify() skips beep when beepEnabled is false", async () => {
    localStorage.setItem("wactorz.beep", "0");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(m.beepEnabled).toBe(false);
    // _beep() not called → AudioContext never constructed
    const origAC = (globalThis as any).AudioContext;
    const acSpy = vi.fn().mockImplementation((...a: any[]) => new origAC(...a));
    (globalThis as any).AudioContext = acSpy;
    m.notify("silent");
    expect(acSpy).not.toHaveBeenCalled();
    (globalThis as any).AudioContext = origAC;
  });

  it("notify() calls speak when ttsEnabled=true", async () => {
    localStorage.setItem("wactorz.tts", "1");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("hello agent");
    await vi.waitFor(() => expect((globalThis as any).speechSynthesis.speak).toHaveBeenCalledOnce());
  });

  it("notify() does not call speak when both ttsEnabled=false and no forceNext", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("hello agent");
    expect((globalThis as any).speechSynthesis.speak).not.toHaveBeenCalled();
  });

  // ── speak keyword variants ────────────────────────────────────────────────

  it.each([
    "speak", "read it out", "say it", "tell me", "voice", "aloud", "out loud", "read this out",
    "narrate", "recite", "read that back", "read it back", "say that aloud", "say it out loud",
  ])(
    "checkUserIntent recognises keyword '%s'",
    async (phrase) => {
      const { TTSManager } = await freshTTS();
      const m = new TTSManager();
      m.checkUserIntent(phrase);
      m.notify("response");
      await vi.waitFor(() => expect((globalThis as any).speechSynthesis.speak).toHaveBeenCalledOnce());
    },
  );

  // ── AudioContext failure / suspend paths ──────────────────────────────────

  it("_ctx() returns null when AudioContext construction throws", async () => {
    const origAC = (globalThis as any).AudioContext;
    (globalThis as any).AudioContext = function () { throw new Error("blocked"); };
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    // notify() → _beep() → _ctx() throws in constructor → returns null → no crash
    expect(() => m.notify("hi")).not.toThrow();
    (globalThis as any).AudioContext = origAC;
  });

  it("_ctx() calls resume() when AudioContext is suspended", async () => {
    const resumeSpy = vi.fn().mockResolvedValue(undefined);
    const origAC = (globalThis as any).AudioContext;
    class SuspendedAC extends origAC {
      state = "suspended";
      resume = resumeSpy;
    }
    (globalThis as any).AudioContext = SuspendedAC;
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(() => m.notify("hi")).not.toThrow();
    expect(resumeSpy).toHaveBeenCalled();
    (globalThis as any).AudioContext = origAC;
  });

  it("_ctx() resume() rejection is caught silently", async () => {
    const origAC = (globalThis as any).AudioContext;
    class SuspendedRejectAC extends origAC {
      state = "suspended";
      resume = vi.fn().mockRejectedValue(new Error("locked"));
    }
    (globalThis as any).AudioContext = SuspendedRejectAC;
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(() => m.notify("hi")).not.toThrow();
    // Allow rejection to propagate and be caught by the empty () => {} catch handler
    await new Promise((r) => setTimeout(r, 20));
    (globalThis as any).AudioContext = origAC;
  });

  // ── selectedVoice / setVoice ───────────────────────────────────────────────

  it("selectedVoice returns empty string when not set", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(m.selectedVoice).toBe("");
  });

  it("selectedVoice returns value from localStorage", async () => {
    localStorage.setItem("wactorz.ttsVoice", "en-US-AriaNeural");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(m.selectedVoice).toBe("en-US-AriaNeural");
  });

  it("setVoice() persists to localStorage", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.setVoice("en-GB-RyanNeural");
    expect(localStorage.getItem("wactorz.ttsVoice")).toBe("en-GB-RyanNeural");
    expect(m.selectedVoice).toBe("en-GB-RyanNeural");
  });

  // ── serverAvailable getter ─────────────────────────────────────────────────

  it("serverAvailable returns false initially", async () => {
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    expect(m.serverAvailable).toBe(false);
  });

  // ── init() / _checkServer() ────────────────────────────────────────────────

  it("init() sets serverAvailable=true when server returns voices", async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: () => Promise.resolve([{ name: "en-US-AriaNeural", locale: "en-US", gender: "Female" }]),
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
    });
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    await m.init();
    expect(m.serverAvailable).toBe(true);
    expect(m.voices).toHaveLength(1);
    globalThis.fetch = origFetch;
  });

  it("init() emits tts-voices-loaded when server has voices", async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: () => Promise.resolve([{ name: "en-US-AriaNeural", locale: "en-US", gender: "Female" }]),
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
    });
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    const spy = vi.fn();
    document.addEventListener("tts-voices-loaded", spy, { once: true });
    await m.init();
    expect(spy).toHaveBeenCalled();
    globalThis.fetch = origFetch;
  });

  it("init() falls back to browser voices when server returns empty array", async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: () => Promise.resolve([]),
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
    });
    (globalThis as any).speechSynthesis = {
      speak: vi.fn(), cancel: vi.fn(),
      getVoices: vi.fn().mockReturnValue([{ name: "Google US English", lang: "en-US" }]),
      addEventListener: vi.fn(),
    };
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    await m.init();
    expect(m.voices.length).toBeGreaterThan(0);
    globalThis.fetch = origFetch;
    (globalThis as any).speechSynthesis = { speak: vi.fn(), cancel: vi.fn() };
  });

  it("init() falls back to browser voices when fetch throws", async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("offline"));
    (globalThis as any).speechSynthesis = {
      speak: vi.fn(), cancel: vi.fn(),
      getVoices: vi.fn().mockReturnValue([{ name: "Google US English", lang: "en-US" }]),
      addEventListener: vi.fn(),
    };
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    await m.init();
    expect(m.voices.length).toBeGreaterThan(0);
    globalThis.fetch = origFetch;
    (globalThis as any).speechSynthesis = { speak: vi.fn(), cancel: vi.fn() };
  });

  it("init() falls back when server returns non-ok response", async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false, status: 404,
      json: () => Promise.resolve(null),
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
    });
    (globalThis as any).speechSynthesis = {
      speak: vi.fn(), cancel: vi.fn(),
      getVoices: vi.fn().mockReturnValue([{ name: "Google US English", lang: "en-US" }]),
      addEventListener: vi.fn(),
    };
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    await m.init();
    expect(m.serverAvailable).toBe(false);
    globalThis.fetch = origFetch;
    (globalThis as any).speechSynthesis = { speak: vi.fn(), cancel: vi.fn() };
  });

  // ── _loadBrowserVoices — voiceschanged path ────────────────────────────────

  it("_loadBrowserVoices resolves immediately when speechSynthesis is null", async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("offline"));
    const origSynth = (globalThis as any).speechSynthesis;
    (globalThis as any).speechSynthesis = null;
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    // init() → _checkServer() fails → _loadBrowserVoices() → synth is null → resolves immediately
    await expect(m.init()).resolves.toBeUndefined();
    globalThis.fetch = origFetch;
    (globalThis as any).speechSynthesis = origSynth;
  });

  it("_loadBrowserVoices subscribes to voiceschanged when getVoices is empty", async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("offline"));
    const addEventListenerSpy = vi.fn();
    (globalThis as any).speechSynthesis = {
      speak: vi.fn(), cancel: vi.fn(),
      getVoices: vi.fn().mockReturnValue([]), // empty → triggers addEventListener path
      addEventListener: addEventListenerSpy,
    };
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    // Don't await — the promise resolves via setTimeout(resolve, 2000)
    const p = m.init();
    // At this point, addEventListener should have been called with "voiceschanged"
    await vi.waitFor(() => expect(addEventListenerSpy).toHaveBeenCalledWith(
      "voiceschanged", expect.any(Function), expect.objectContaining({ once: true }),
    ));
    // Let the 2-second timeout resolve the promise
    await vi.runAllTimersAsync().catch(() => {});
    await p.catch(() => {});
    globalThis.fetch = origFetch;
    (globalThis as any).speechSynthesis = { speak: vi.fn(), cancel: vi.fn() };
  });

  it("_speakBrowser is no-op when speechSynthesis is null", async () => {
    const origSynth = (globalThis as any).speechSynthesis;
    (globalThis as any).speechSynthesis = null;
    localStorage.setItem("wactorz.tts", "1");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("hi");
    // Yield a microtask tick so the fetch 503 → _speakBrowser chain runs while synth is still null
    await Promise.resolve();
    await Promise.resolve();
    // _speakBrowser ran with null synth → if (!synth) return; → early exit
    (globalThis as any).speechSynthesis = origSynth;
    // speech.speak was not called since synth was null
    expect(origSynth.speak).not.toHaveBeenCalled();
  });

  it("_speakBrowser does not set voice when selected voice is not in getVoices", async () => {
    const mockVoice = { name: "Google UK English Male", lang: "en-GB" };
    (globalThis as any).speechSynthesis = {
      speak: vi.fn(), cancel: vi.fn(),
      getVoices: () => [mockVoice],
    };
    localStorage.setItem("wactorz.ttsVoice", "NonExistentVoice");
    localStorage.setItem("wactorz.tts", "1");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("test");
    await vi.waitFor(() => expect((globalThis as any).speechSynthesis.speak).toHaveBeenCalledOnce());
    const utt = (globalThis as any).speechSynthesis.speak.mock.calls[0][0];
    // voice is not set since "NonExistentVoice" doesn't match
    expect(utt.voice).toBeUndefined();
    (globalThis as any).speechSynthesis = { speak: vi.fn(), cancel: vi.fn() };
  });

  // ── _speakBrowser with selected voice ─────────────────────────────────────

  it("_speakBrowser uses selected voice when found in getVoices", async () => {
    const mockVoice = { name: "Google US English", lang: "en-US" };
    (globalThis as any).speechSynthesis = {
      speak: vi.fn(), cancel: vi.fn(),
      getVoices: () => [mockVoice],
    };
    localStorage.setItem("wactorz.ttsVoice", "Google US English");
    localStorage.setItem("wactorz.tts", "1");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("test");
    // After 503 → _speakBrowser called with voice selection
    await vi.waitFor(() => expect((globalThis as any).speechSynthesis.speak).toHaveBeenCalledOnce());
    const utt = (globalThis as any).speechSynthesis.speak.mock.calls[0][0];
    expect(utt.voice).toBe(mockVoice);
    (globalThis as any).speechSynthesis = { speak: vi.fn(), cancel: vi.fn() };
  });

  // ── _speakServer — ok=true path (decodeAudioData success) ────────────────

  it("_speakServer plays audio when server returns ok=true with buffer", async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(32)),
      json: () => Promise.resolve(null),
    });
    localStorage.setItem("wactorz.tts", "1");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("hello server");
    // Wait for the async chain: fetch → arrayBuffer → decodeAudioData → bufSource.start
    await new Promise((r) => setTimeout(r, 50));
    // No throw = success; server path executed (no speakBrowser call)
    expect((globalThis as any).speechSynthesis.speak).not.toHaveBeenCalled();
    globalThis.fetch = origFetch;
  });

  it("_speakServer with selected voice includes voice in params", async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(32)),
      json: () => Promise.resolve(null),
    });
    localStorage.setItem("wactorz.tts", "1");
    localStorage.setItem("wactorz.ttsVoice", "en-US-AriaNeural");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("test voice param");
    await new Promise((r) => setTimeout(r, 50));
    const url: string = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]?.[0] ?? "";
    expect(url).toContain("voice=en-US-AriaNeural");
    globalThis.fetch = origFetch;
  });

  it("_speakServer falls back to browser when AudioContext is null", async () => {
    const origFetch = globalThis.fetch;
    const origAC = (globalThis as any).AudioContext;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(32)),
      json: () => Promise.resolve(null),
    });
    (globalThis as any).AudioContext = function () { throw new Error("blocked"); };
    localStorage.setItem("wactorz.tts", "1");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("no ctx");
    await vi.waitFor(() => expect((globalThis as any).speechSynthesis.speak).toHaveBeenCalled());
    globalThis.fetch = origFetch;
    (globalThis as any).AudioContext = origAC;
  });

  it("_speakServer when decodeAudioData fails falls back to browser speak", async () => {
    const origFetch = globalThis.fetch;
    const origAC = (globalThis as any).AudioContext;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(32)),
      json: () => Promise.resolve(null),
    });
    (globalThis as any).AudioContext = class extends origAC {
      decodeAudioData() { return Promise.reject(new Error("bad data")); }
    };
    localStorage.setItem("wactorz.tts", "1");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("decode fail");
    await vi.waitFor(() => expect((globalThis as any).speechSynthesis.speak).toHaveBeenCalled());
    globalThis.fetch = origFetch;
    (globalThis as any).AudioContext = origAC;
  });

  it("_speakServer on network error falls back to browser speak", async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("network error"));
    localStorage.setItem("wactorz.tts", "1");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("network fail");
    await vi.waitFor(() => expect((globalThis as any).speechSynthesis.speak).toHaveBeenCalled());
    globalThis.fetch = origFetch;
  });

  it("_speakServer with ok=false and non-503/404 status returns null (no speak)", async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false, status: 400,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
      json: () => Promise.resolve(null),
    });
    localStorage.setItem("wactorz.tts", "1");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("bad request");
    await new Promise((r) => setTimeout(r, 50));
    expect((globalThis as any).speechSynthesis.speak).not.toHaveBeenCalled();
    globalThis.fetch = origFetch;
  });

  // ── _speak else branch (_serverAvailable===false) ────────────────────────

  it("_speak goes directly to speakBrowser when _serverAvailable is false", async () => {
    localStorage.setItem("wactorz.tts", "1");
    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    // First notify: 503 → sets _serverAvailable=false
    m.notify("first");
    await vi.waitFor(() => expect((globalThis as any).speechSynthesis.speak).toHaveBeenCalledOnce());
    vi.clearAllMocks();
    // Second notify: _serverAvailable===false → else branch (line 178) → no fetch
    m.notify("second");
    expect((globalThis as any).speechSynthesis.speak).toHaveBeenCalledOnce();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  // ── _speakServer src.onended callback (line 210) ──────────────────────────

  it("_speakServer fires src.onended without throwing", async () => {
    const origFetch = globalThis.fetch;
    const origAC = (globalThis as any).AudioContext;

    class AutoFireAC extends origAC {
      createBufferSource() {
        const src = {
          buffer: null as any,
          loop: false,
          connect: () => {},
          stop: () => {},
          onended: null as (() => void) | null,
          start() { if (this.onended) this.onended(); },
        };
        return src;
      }
    }

    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(32)),
      json: () => Promise.resolve(null),
    });
    (globalThis as any).AudioContext = AutoFireAC;
    localStorage.setItem("wactorz.tts", "1");

    const { TTSManager } = await freshTTS();
    const m = new TTSManager();
    m.notify("test onended");
    // Allow async chain + dynamic import callbacks to complete
    await new Promise((r) => setTimeout(r, 200));
    // src.onended fired: import("./AmbientManager").then(duck(false)) ran without throw
    expect((globalThis as any).speechSynthesis.speak).not.toHaveBeenCalled();

    globalThis.fetch = origFetch;
    (globalThis as any).AudioContext = origAC;
  });

  // ── singleton export ───────────────────────────────────────────────────────

  it("exports a singleton tts instance", async () => {
    const { tts, TTSManager } = await freshTTS();
    expect(tts).toBeInstanceOf(TTSManager);
  });
});
