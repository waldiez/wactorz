// Live data layer: MQTT-over-WebSocket feed + temperature→colour mapping.
// Reads the labeler's keyed obs (…/observation/labeled), per-zone actions,
// anomalies and episode events. Nothing here is building-specific — zone names
// arrive in the data and are matched to geometry by name.
import mqtt from "mqtt";
// ── temperature → colour ─────────────────────────────────────────────────────
// blue (cold) → teal → green (comfort) → amber → red (hot). Comfort ≈ 20–26 °C.
const STOPS = [
    [15, [0.23, 0.48, 1.0]],
    [20, [0.21, 0.83, 0.74]],
    [23, [0.22, 0.83, 0.62]],
    [26, [0.95, 0.82, 0.23]],
    [30, [1.0, 0.33, 0.44]],
];
export function tempColor(t) {
    if (t <= STOPS[0][0])
        return STOPS[0][1];
    if (t >= STOPS[STOPS.length - 1][0])
        return STOPS[STOPS.length - 1][1];
    for (let i = 0; i < STOPS.length - 1; i++) {
        const [a, ca] = STOPS[i], [b, cb] = STOPS[i + 1];
        if (t >= a && t <= b) {
            const f = (t - a) / (b - a);
            return [ca[0] + (cb[0] - ca[0]) * f, ca[1] + (cb[1] - ca[1]) * f, ca[2] + (cb[2] - ca[2]) * f];
        }
    }
    return STOPS[STOPS.length - 1][1];
}
export const cssColor = (c) => `rgb(${(c[0] * 255) | 0},${(c[1] * 255) | 0},${(c[2] * 255) | 0})`;
export class Feed {
    frameCbs = [];
    alertCbs = [];
    statusCbs = [];
    /** zone_index → zone name, learned live from the per-zone action topic. */
    zoneByIdx = {};
    onFrame(cb) { this.frameCbs.push(cb); }
    onAlert(cb) { this.alertCbs.push(cb); }
    onStatus(cb) { this.statusCbs.push(cb); }
    connect(url = `ws://${location.hostname}:9001`) {
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
    handle(topic, buf) {
        let msg;
        try {
            msg = JSON.parse(new TextDecoder().decode(buf));
        }
        catch {
            return;
        }
        const parts = topic.split("/");
        const envId = parts[2] ?? "";
        if (topic.endsWith("/observation/labeled"))
            this.emitFrame(envId, msg);
        else if (topic.endsWith("/anomaly"))
            this.emitAlert(msg, false);
        else if (topic.endsWith("/action") && msg.zone && typeof msg.zone_index === "number") {
            this.zoneByIdx[msg.zone_index] = msg.zone;
        }
    }
    emitFrame(envId, m) {
        const zoneTemp = {};
        for (const k of Object.keys(m)) {
            if (k.startsWith("air_temperature_"))
                zoneTemp[k.slice("air_temperature_".length)] = m[k];
        }
        const info = m.info ?? {};
        const f = {
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
            zoneTemp,
            faultActive: !!info.hvac_fault_active,
            hvacEff: typeof info.hvac_efficiency === "number" ? info.hvac_efficiency : null,
        };
        this.frameCbs.forEach((cb) => cb(f));
    }
    emitAlert(m, groundTruth) {
        const a = {
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
