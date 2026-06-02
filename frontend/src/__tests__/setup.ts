// Global test setup — runs before every test file.
// happy-dom provides window/document/localStorage/etc.

// Stub out AudioContext (happy-dom doesn't implement Web Audio API)
class MockAudioContext {
  state = "running";
  currentTime = 0;
  destination = {};
  createOscillator() {
    return {
      type: "sine" as OscillatorType,
      frequency: { value: 0 },
      connect: () => {},
      start: () => {},
      stop: () => {},
    };
  }
  createGain() {
    return {
      gain: { value: 1, setTargetAtTime: () => {} },
      connect: () => {},
    };
  }
  createBufferSource() {
    return {
      buffer: null as any,
      loop: false,
      connect: () => {},
      start: () => {},
      stop: () => {},
      onended: null as any,
    };
  }
  decodeAudioData(_buf: ArrayBuffer): Promise<any> {
    return Promise.resolve({});
  }
  resume() { return Promise.resolve(); }
  close()  { return Promise.resolve(); }
}
(globalThis as any).AudioContext = MockAudioContext;

// Stub speechSynthesis
(globalThis as any).speechSynthesis = {
  speak: vi.fn(),
  cancel: vi.fn(),
};

// Stub SpeechSynthesisUtterance
(globalThis as any).SpeechSynthesisUtterance = class {
  rate = 1; pitch = 1; volume = 1;
  constructor(public text: string) {}
};

// Stub requestAnimationFrame for ToastManager animations
globalThis.requestAnimationFrame = (cb: FrameRequestCallback) => {
  cb(0);
  return 0;
};

// Stub fetch — default 503 so TTS falls back to browser speech synthesis
globalThis.fetch = vi.fn().mockResolvedValue({
  ok: false,
  status: 503,
  json: () => Promise.resolve([]),
  arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
});
