// History replay: pulls a decimated timeline from /api/history and scrubs/plays it.
// Faithful fields for this env are the agents' setpoints (all 15 zones); true
// temperature is only reliable for the 3 core zones (see README — bridge RDF bug).
export type Metric = "clg" | "htg" | "temp";

export interface History {
  episode: number; steps: number[]; stride: number;
  zones: string[]; tempZones: string[];
  htg: Record<string, (number | null)[]>;
  clg: Record<string, (number | null)[]>;
  temp: Record<string, (number | null)[]>;
  anomalies: { step: number; kind: string; severity: number; zoneIdx: number | null }[];
}

export interface HFrame {
  i: number; step: number;
  values: Record<string, number | null>;   // zone → scalar for the active metric
  metric: Metric;
}

export class HistoryPlayer {
  private h?: History;
  private i = 0;
  private timer?: number;
  metric: Metric = "temp";
  fps = 12;
  onFrame?: (f: HFrame) => void;

  setFps(n: number) {
    this.fps = n;
    if (this.timer) { this.pause(); this.play(); }   // restart at new cadence
  }

  get loaded() { return !!this.h; }
  get length() { return this.h?.steps.length ?? 0; }
  get index() { return this.i; }
  get data() { return this.h; }

  async load(frames = 720): Promise<History> {
    const h: History = await fetch(`/api/history?frames=${frames}`).then((r) => r.json());
    this.h = h; this.i = 0;
    this.emit();
    return h;
  }

  setMetric(m: Metric) { this.metric = m; this.emit(); }

  seek(i: number) {
    if (!this.h) return;
    this.i = Math.max(0, Math.min(this.h.steps.length - 1, i));
    this.emit();
  }

  play() {
    if (!this.h || this.timer) return;
    this.timer = window.setInterval(() => {
      if (!this.h) return;
      this.i++;
      if (this.i >= this.h.steps.length) { this.i = 0; }
      this.emit();
    }, 1000 / this.fps);
  }

  pause() { if (this.timer) { clearInterval(this.timer); this.timer = undefined; } }
  get playing() { return !!this.timer; }

  private emit() {
    if (!this.h || !this.onFrame) return;
    const src = this.h[this.metric];
    const values: Record<string, number | null> = {};
    for (const z of this.h.zones) values[z] = src[z]?.[this.i] ?? null;
    this.onFrame({ i: this.i, step: this.h.steps[this.i], values, metric: this.metric });
  }
}
