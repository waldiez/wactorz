// DOM dashboard: weather rail, clock, zone list, anomaly feed, floor toggles.
import type { Alert, Frame, Geometry } from "./data";
import { cssColor, tempColor } from "./data";
import type { HFrame } from "./history";
import type { Twin } from "./twin";

const MONTHS = ["—", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DOM = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
const $ = (id: string) => document.getElementById(id)!;
const fmt = (v: number, d = 1) => (Number.isFinite(v) ? v.toFixed(d) : "—");

// EnergyPlus step (15-min) → calendar month/day/hour (non-leap).
function stepDate(step: number): { month: number; day: number; hour: number } {
  let doy = Math.floor(step / 96);            // 0-based day of year
  const hour = Math.floor((step % 96) / 4);
  let m = 0;
  while (m < 12 && doy >= DOM[m]) { doy -= DOM[m]; m++; }
  return { month: m + 1, day: doy + 1, hour };
}

export class UI {
  private rows = new Map<string, { root: HTMLElement; temp: HTMLElement; swatch: HTMLElement }>();
  private alertCount = 0;

  constructor(private geo: Geometry, private twin: Twin) {
    this.buildZoneList();
    this.buildFloorToggles();
  }

  setConn(ok: boolean) {
    $("conn-text").textContent = ok ? "live · mqtt" : "reconnecting…";
    $("conn").querySelector(".led")!.className = "led " + (ok ? "on" : "off");
  }

  frame(f: Frame) {
    // clock
    $("clock").textContent =
      `${MONTHS[f.month] ?? "—"} ${f.day || ""} · ${String(f.hour).padStart(2, "0")}:00 · ep ${f.episode}`;
    // weather
    $("wx-temp").textContent = fmt(f.outdoor.temp);
    $("wx-hum").textContent = `${fmt(f.outdoor.hum, 0)} %`;
    $("wx-wind").textContent = `${fmt(f.outdoor.wind)} m/s`;
    $("wx-dir").textContent = `${fmt(f.outdoor.dir, 0)}°`;
    $("wx-dif").textContent = `${fmt(f.outdoor.dif, 0)} W/m²`;
    $("wx-dir-sun").textContent = `${fmt(f.outdoor.dir_sun, 0)} W/m²`;
    ($("wx-dif-bar") as HTMLElement).style.width = `${Math.min(100, f.outdoor.dif / 5)}%`;
    ($("wx-dir-bar") as HTMLElement).style.width = `${Math.min(100, f.outdoor.dir_sun / 9)}%`;
    $("hvac").textContent = Number.isFinite(f.hvacW) ? `${(f.hvacW / 1000).toFixed(1)} kW` : "—";

    // zones
    const temps = Object.values(f.zoneTemp).filter(Number.isFinite);
    const mean = temps.length ? temps.reduce((a, b) => a + b, 0) / temps.length : NaN;
    $("meanzone").textContent = `${fmt(mean)} °C`;
    const inBand = temps.filter((t) => t >= 20 && t <= 26).length;
    $("comfort").textContent = temps.length ? `${((inBand / temps.length) * 100) | 0}%` : "—";
    if (f.faultActive) {
      $("hvac").innerHTML = `${Number.isFinite(f.hvacW) ? (f.hvacW / 1000).toFixed(1) : "—"} kW ` +
        `<span style="color:var(--warn)">⚠ fault</span>`;
    }

    for (const [zone, t] of Object.entries(f.zoneTemp)) {
      this.twin.setZoneTemp(zone, t);
      const r = this.rows.get(zone);
      if (r) {
        r.temp.textContent = `${fmt(t)}°`;
        r.temp.style.color = cssColor(tempColor(t));
        r.swatch.style.background = cssColor(tempColor(t));
      }
    }
  }

  alert(a: Alert, zoneByIdx: Record<number, string>) {
    this.alertCount++;
    $("acount").textContent = String(this.alertCount);
    const zoneName = a.zoneIdx != null ? zoneByIdx[a.zoneIdx] : undefined;
    if (zoneName) {
      this.twin.flashZone(zoneName);
      this.rows.get(zoneName)?.root.classList.add("alarm");
      setTimeout(() => this.rows.get(zoneName)?.root.classList.remove("alarm"), 4000);
    }
    const feed = $("alertfeed");
    feed.querySelector(".empty")?.remove();
    const el = document.createElement("div");
    el.className = "alert";
    const sevCls = a.groundTruth ? "sev gt" : "sev";
    el.innerHTML =
      `<span class="${sevCls}">${a.groundTruth ? "TRUTH" : (a.severity * 100 | 0) + "%"}</span>` +
      `<span class="kind">${a.kind}${zoneName ? " · " + zoneName : ""}</span>` +
      `<span class="meta">ep${a.episode} · step ${a.step}</span>`;
    feed.prepend(el);
    while (feed.children.length > 40) feed.lastChild?.remove();
  }

  private badge?: HTMLElement;
  setMode(mode: "live" | "history") {
    if (!this.badge) {
      this.badge = document.createElement("div");
      this.badge.className = "mode-badge";
      document.getElementById("stage")!.appendChild(this.badge);
    }
    this.badge.textContent = mode === "history" ? "▷ REPLAY" : "● LIVE";
    this.badge.style.display = mode === "history" ? "block" : "none";
  }

  historyFrame(f: HFrame, total: number) {
    const d = stepDate(f.step);
    $("clock").textContent =
      `${MONTHS[d.month]} ${d.day} · ${String(d.hour).padStart(2, "0")}:00 · replay`;
    $("tp-frame").textContent =
      `${MONTHS[d.month]} ${d.day} ${String(d.hour).padStart(2, "0")}:00 · ${f.i + 1}/${total}`;
    const unit = f.metric === "temp" ? "°C" : "°C set";
    const vals: number[] = [];
    for (const [zone, v] of Object.entries(f.values)) {
      if (v == null) continue;
      vals.push(v);
      this.twin.setZoneTemp(zone, v);
      const r = this.rows.get(zone);
      if (r) {
        r.temp.textContent = `${v.toFixed(1)}°`;
        r.temp.style.color = cssColor(tempColor(v));
        r.swatch.style.background = cssColor(tempColor(v));
      }
    }
    const mean = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : NaN;
    $("meanzone").textContent = `${fmt(mean)} ${unit}`;
  }

  private buildZoneList() {
    const list = $("zonelist");
    const occ = Object.entries(this.geo.zones).filter(([, z]) => z.occupied);
    $("zcount").textContent = `${occ.length}`;
    // order by floor then name for a tidy list
    occ.sort((a, b) => (a[1].floor + a[0]).localeCompare(b[1].floor + b[0]));
    for (const [name] of occ) {
      const row = document.createElement("div");
      row.className = "zrow";
      const swatch = document.createElement("i"); swatch.className = "swatch";
      const nm = document.createElement("span"); nm.className = "zname"; nm.textContent = name;
      const tp = document.createElement("span"); tp.className = "ztemp"; tp.textContent = "—";
      row.append(swatch, nm, tp);
      list.appendChild(row);
      this.rows.set(name, { root: row, temp: tp, swatch });
    }
  }

  private buildFloorToggles() {
    const box = $("floors");
    for (const floor of this.geo.floors) {
      const b = document.createElement("button");
      b.textContent = floor;
      let on = true;
      b.onclick = () => {
        on = !on; b.classList.toggle("off", !on);
        this.twin.setFloorVisible(floor, on);
      };
      box.appendChild(b);
    }
    // glazing toggle (only if the building has windows)
    if (this.geo.windows?.length) {
      const g = document.createElement("button");
      g.textContent = "✦ glass";
      g.className = "glass-toggle";
      let on = true;
      g.onclick = () => {
        on = !on; g.classList.toggle("off", !on);
        this.twin.setWindowsVisible(on);
      };
      box.appendChild(g);
    }
  }
}
