// Live data layer: MQTT-over-WebSocket feed + temperature→colour mapping.
// Reads the labeler's keyed obs (…/observation/labeled), per-zone actions,
// anomalies and episode events. Nothing here is building-specific — zone names
// arrive in the data and are matched to geometry by name.
import mqtt from "mqtt";

export interface Geometry {
  building: string;
  bbox: { min: [number, number, number]; max: [number, number, number] };
  floors: string[];
  zones: Record<string, ZoneGeo>;
  windows: { zone: string | null; surface: string; kind: string; vertices: number[][] }[];
}
export interface ZoneGeo {
  occupied: boolean;
  floor: string;
  z_range: [number, number];
  centroid: [number, number, number];
  surfaces: { type: string; vertices: number[][] }[];
}

export interface Frame {
  envId: string;
  step: number;
  episode: number;
  month: number; day: number; hour: number;
  outdoor: { temp: number; hum: number; wind: number; dir: number; dif: number; dir_sun: number };
  hvacW: number;
  elecTotalW: number;                 // facility total HVAC electricity
  zoneTemp: Record<string, number>;   // by zone name
  zoneHum: Record<string, number>;    // relative humidity %
  zoneOcc: Record<string, number>;    // people occupant count
  zoneHtg: Record<string, number>;    // heating setpoint °C
  zoneClg: Record<string, number>;    // cooling setpoint °C
  occTotal: number;                   // people across the building
  raining: boolean;                   // Sinergym info.is_raining
  faultActive: boolean;
  hvacEff: number | null;
}

export interface Alert {
  step: number; episode: number; kind: string; severity: number;
  zoneIdx: number | null; message: string; detector: string; groundTruth?: boolean;
}

// ── colour ramps ─────────────────────────────────────────────────────────────
function ramp(stops: [number, [number, number, number]][], v: number): [number, number, number] {
  if (!Number.isFinite(v) || v <= stops[0][0]) return stops[0][1];
  if (v >= stops[stops.length - 1][0]) return stops[stops.length - 1][1];
  for (let i = 0; i < stops.length - 1; i++) {
    const [a, ca] = stops[i], [b, cb] = stops[i + 1];
    if (v >= a && v <= b) {
      const f = (v - a) / (b - a);
      return [ca[0] + (cb[0] - ca[0]) * f, ca[1] + (cb[1] - ca[1]) * f, ca[2] + (cb[2] - ca[2]) * f];
    }
  }
  return stops[stops.length - 1][1];
}

// Temperature: steep, saturated stops through the occupied band (20–26 °C) so small
// differences between zones read as clearly distinct colours (blue→cyan→green→amber→red).
const TEMP_STOPS: [number, [number, number, number]][] = [
  [16, [0.18, 0.42, 1.0]],
  [20, [0.14, 0.80, 0.95]],
  [22, [0.16, 0.86, 0.52]],
  [24, [0.88, 0.86, 0.20]],
  [26, [1.0, 0.55, 0.16]],
  [29, [1.0, 0.26, 0.40]],
];
export const tempColor = (t: number) => ramp(TEMP_STOPS, t);

// Occupancy: dim slate when empty → warm amber → near-white as a zone fills (≈8 people).
const OCC_STOPS: [number, [number, number, number]][] = [
  [0, [0.15, 0.19, 0.31]],
  [1, [0.85, 0.52, 0.18]],
  [4, [1.0, 0.72, 0.28]],
  [8, [1.0, 0.93, 0.72]],
];
export const occColor = (people: number) => ramp(OCC_STOPS, people);
export const cssColor = (c: [number, number, number]) =>
  `rgb(${(c[0] * 255) | 0},${(c[1] * 255) | 0},${(c[2] * 255) | 0})`;

type FrameCb = (f: Frame) => void;
type AlertCb = (a: Alert) => void;
type StatusCb = (connected: boolean) => void;

export class Feed {
  private frameCbs: FrameCb[] = [];
  private alertCbs: AlertCb[] = [];
  private statusCbs: StatusCb[] = [];
  /** zone_index → zone name, learned live from the per-zone action topic. */
  readonly zoneByIdx: Record<number, string> = {};

  onFrame(cb: FrameCb) { this.frameCbs.push(cb); }
  onAlert(cb: AlertCb) { this.alertCbs.push(cb); }
  onStatus(cb: StatusCb) { this.statusCbs.push(cb); }

  connect(port = 9001) {
    const url = `ws://${location.hostname}:${port}`;
    const c = mqtt.connect(url, { reconnectPeriod: 2000, connectTimeout: 8000 });
    c.on("connect", () => {
      this.statusCbs.forEach((cb) => cb(true));
      c.subscribe([
        "sinergym/env/+/observation/labeled",
        "sinergym/env/+/zone/+/action",
        "sinergym/env/+/anomaly",
        "sinergym/env/+/episode",
      ]);
    });
    c.on("reconnect", () => this.statusCbs.forEach((cb) => cb(false)));
    c.on("close", () => this.statusCbs.forEach((cb) => cb(false)));
    c.on("error", () => this.statusCbs.forEach((cb) => cb(false)));
    c.on("message", (topic, buf) => this.handle(topic, buf));
  }

  private handle(topic: string, buf: Uint8Array) {
    let msg: any;
    try { msg = JSON.parse(new TextDecoder().decode(buf)); } catch { return; }
    const parts = topic.split("/");
    const envId = parts[2] ?? "";
    if (topic.endsWith("/observation/labeled")) this.emitFrame(envId, msg);
    else if (topic.endsWith("/anomaly")) this.emitAlert(msg, false);
    else if (topic.endsWith("/action") && msg.zone && typeof msg.zone_index === "number") {
      this.zoneByIdx[msg.zone_index] = msg.zone;
    }
  }

  private emitFrame(envId: string, m: Record<string, any>) {
    // The labeler republishes every obs as <variable>_<zone>; bucket the per-zone
    // families we care about by prefix so nothing here is building-specific.
    const zoneTemp: Record<string, number> = {};
    const zoneHum: Record<string, number> = {};
    const zoneOcc: Record<string, number> = {};
    const zoneHtg: Record<string, number> = {};
    const zoneClg: Record<string, number> = {};
    const bucket = (k: string, p: string, dst: Record<string, number>) => {
      if (k.startsWith(p)) { dst[k.slice(p.length)] = m[k]; return true; }
      return false;
    };
    for (const k of Object.keys(m)) {
      bucket(k, "air_temperature_", zoneTemp) ||
        bucket(k, "air_humidity_", zoneHum) ||
        bucket(k, "people_occupant_", zoneOcc) ||
        bucket(k, "htg_setpoint_", zoneHtg) ||
        bucket(k, "clg_setpoint_", zoneClg);
    }
    const occTotal = Object.values(zoneOcc).reduce((a, b) => a + (Number.isFinite(b) ? b : 0), 0);
    const info = m.info ?? {};
    const f: Frame = {
      envId,
      step: m.step ?? 0,
      episode: m.episode ?? 0,
      month: m.month ?? 0, day: m.day_of_month ?? 0, hour: m.hour ?? 0,
      outdoor: {
        temp: m.outdoor_temperature ?? NaN, hum: m.outdoor_humidity ?? NaN,
        wind: m.wind_speed ?? NaN, dir: m.wind_direction ?? NaN,
        dif: m.diffuse_solar_radiation ?? 0, dir_sun: m.direct_solar_radiation ?? 0,
      },
      hvacW: m.HVAC_electricity_demand_rate ?? NaN,
      elecTotalW: m.total_electricity_HVAC ?? NaN,
      zoneTemp, zoneHum, zoneOcc, zoneHtg, zoneClg, occTotal,
      raining: !!(info.is_raining ?? m.is_raining),
      faultActive: !!info.hvac_fault_active,
      hvacEff: typeof info.hvac_efficiency === "number" ? info.hvac_efficiency : null,
    };
    this.frameCbs.forEach((cb) => cb(f));
  }

  private emitAlert(m: Record<string, any>, groundTruth: boolean) {
    const a: Alert = {
      step: m.step ?? 0, episode: m.episode ?? 0,
      kind: m.kind_guess ?? m.kind ?? "anomaly",
      severity: m.severity ?? 0,
      zoneIdx: m.zone_idx ?? null,
      message: m.message ?? "",
      detector: m.detector ?? "",
      groundTruth,
    };
    this.alertCbs.forEach((cb) => cb(a));
  }
}
