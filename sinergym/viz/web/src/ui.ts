// DOM dashboard: instrument rail, per-floor zone matrix, occupancy, anomaly feed.
import type { Alert, Frame, Geometry } from "./data";
import { cssColor, occColor, tempColor } from "./data";
import type { HFrame } from "./history";
import type { Twin } from "./twin";

const MONTHS = ["—", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DOM = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
const FLOOR_LABEL: Record<string, string> = { bottom: "Ground", mid: "Mid", top: "Top" };
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

// "Core_mid" → "Core", "Perimeter_bot_ZN_3" → "P3" (floor is the group header).
function zoneAbbr(name: string): string {
  const m = name.match(/ZN_?(\d+)/i);
  if (m) return "P" + m[1];
  if (/core/i.test(name)) return "Core";
  return (name.split("_").pop() ?? name).slice(0, 5);
}
// sort cores first, then perimeter by index
function zoneSort(a: string, b: string): number {
  const ca = /core/i.test(a) ? 0 : 1, cb = /core/i.test(b) ? 0 : 1;
  if (ca !== cb) return ca - cb;
  const na = +(a.match(/ZN_?(\d+)/i)?.[1] ?? 0), nb = +(b.match(/ZN_?(\d+)/i)?.[1] ?? 0);
  return na - nb;
}

interface Cell {
  root: HTMLElement; name: HTMLElement; temp: HTMLElement; pips: HTMLElement; lastOcc: number;
}

export class UI {
  private cells = new Map<string, Cell>();
  private alertCount = 0;
  private colorMode: "temp" | "occ" = "temp";
  private lastFrame?: Frame;

  constructor(private geo: Geometry, private twin: Twin) {
    this.buildZoneMatrix();
    this.buildFloorToggles();
    const sel = document.getElementById("view-metric") as HTMLSelectElement | null;
    sel?.addEventListener("change", () => {
      this.colorMode = (sel.value as "temp" | "occ");
      if (this.lastFrame) this.recolor(this.lastFrame);   // repaint immediately
    });
  }

  private zoneColor(zone: string, f: Frame): [number, number, number] {
    return this.colorMode === "occ"
      ? occColor(f.zoneOcc[zone] ?? 0)
      : tempColor(f.zoneTemp[zone]);
  }
  private recolor(f: Frame) {
    for (const zone of Object.keys(f.zoneTemp)) this.twin.setZoneColor(zone, this.zoneColor(zone, f));
  }

  setConn(ok: boolean) {
    $("conn-text").textContent = ok ? "live · mqtt" : "reconnecting…";
    $("conn").querySelector(".led")!.className = "led " + (ok ? "on" : "off");
  }

  // Make day/night unmistakable without touching the temperature tints: a masthead
  // sun/moon chip + a soft sky wash layered *over* the canvas (so zone colours are untouched).
  private setPhase(hour: number) {
    const h = (((hour % 24) + 24) % 24);
    let day = 0;                                   // 0 night … 1 midday, smooth at the edges
    if (h >= 8 && h <= 16) day = 1;
    else if (h > 4 && h < 8) day = (h - 4) / 4;
    else if (h > 16 && h < 20) day = (20 - h) / 4;
    let label = "night", ico = "☾", cls = "night";
    if (h >= 7 && h < 9) { label = "dawn"; ico = "🌅"; cls = "dawn"; }
    else if (h >= 9 && h < 17) { label = "day"; ico = "☀"; cls = "day"; }
    else if (h >= 17 && h < 19) { label = "dusk"; ico = "🌆"; cls = "dusk"; }
    const cue = $("daycue");
    cue.className = "daycue " + cls;
    cue.querySelector(".daycue-ico")!.textContent = ico;
    $("daycue-label").textContent = label;
    $("stage").style.setProperty("--daylight", day.toFixed(2));
  }

  private setSky(raining: boolean) {
    const el = $("wx-sky");
    el.className = "wx-sky" + (raining ? " rain" : "");
    el.innerHTML = raining
      ? `<span class="wx-sky-ico">☔</span> raining`
      : `<span class="wx-sky-ico">☀</span> clear`;
    document.getElementById("stage")!.classList.toggle("raining", raining);
  }

  frame(f: Frame) {
    this.lastFrame = f;
    this.twin.setTimeOfDay(f.hour);
    this.twin.setRaining(f.raining);
    this.setSky(f.raining);
    this.setPhase(f.hour);
    const d = `${MONTHS[f.month] ?? "—"} ${f.day || ""} · ${String(f.hour).padStart(2, "0")}:00`;
    $("clock").textContent = `${d} · ep ${f.episode}`;
    // outdoor
    $("wx-temp").textContent = fmt(f.outdoor.temp);
    $("wx-hum").textContent = `${fmt(f.outdoor.hum, 0)} %`;
    $("wx-wind").textContent = `${fmt(f.outdoor.wind)} m/s`;
    $("wx-dir").textContent = `${fmt(f.outdoor.dir, 0)}°`;
    $("wx-dif").textContent = `${fmt(f.outdoor.dif, 0)} W/m²`;
    $("wx-dir-sun").textContent = `${fmt(f.outdoor.dir_sun, 0)} W/m²`;
    ($("wx-dif-bar") as HTMLElement).style.width = `${Math.min(100, f.outdoor.dif / 5)}%`;
    ($("wx-dir-bar") as HTMLElement).style.width = `${Math.min(100, f.outdoor.dir_sun / 9)}%`;
    // simulation
    $("sim-ep").textContent = String(f.episode);
    $("sim-date").textContent = d.replace(" · ", "  ");
    $("sim-step").textContent = `${f.step.toLocaleString()} / 35,040`;
    // building
    $("hvac").innerHTML = Number.isFinite(f.hvacW)
      ? `${(f.hvacW / 1000).toFixed(1)} kW` + (f.faultActive ? ` <span class="tag-warn">⚠ fault</span>` : "")
      : "—";
    $("elec-total").textContent = Number.isFinite(f.elecTotalW)
      ? `${(f.elecTotalW / 3.6e6).toFixed(2)} kWh` : "—";
    $("occ-total").textContent = String(Math.round(f.occTotal));

    const temps = Object.values(f.zoneTemp).filter(Number.isFinite);
    const mean = temps.length ? temps.reduce((a, b) => a + b, 0) / temps.length : NaN;
    $("meanzone").textContent = `${fmt(mean)} °C`;
    const inBand = temps.filter((t) => t >= 20 && t <= 26).length;
    const cpct = temps.length ? Math.round((inBand / temps.length) * 100) : 0;
    $("comfort").textContent = temps.length ? `${cpct}%` : "—";
    ($("comfort-bar") as HTMLElement).style.width = `${cpct}%`;

    for (const [zone, t] of Object.entries(f.zoneTemp)) {
      const occ = f.zoneOcc[zone] ?? 0;
      this.twin.setZoneColor(zone, this.zoneColor(zone, f));
      this.twin.setZoneOccupancy(zone, occ);
      this.updateCell(zone, t, occ, f);
    }
  }

  private updateCell(zone: string, t: number, occ: number, f: Frame) {
    const c = this.cells.get(zone); if (!c) return;
    const col = cssColor(tempColor(t));
    c.temp.textContent = Number.isFinite(t) ? `${t.toFixed(1)}°` : "—";
    c.temp.style.color = col;
    c.root.style.setProperty("--tc", col);
    c.root.classList.toggle("occupied", occ >= 0.5);
    const ppl = Math.round(occ);
    if (ppl !== c.lastOcc) { this.renderPips(c, ppl); c.lastOcc = ppl; }
    const hum = f.zoneHum[zone], htg = f.zoneHtg[zone], clg = f.zoneClg[zone];
    c.root.title =
      `${zone}\n${fmt(t)} °C · ${fmt(hum, 0)} %RH\n` +
      `setpoints  ${fmt(htg)}–${fmt(clg)} °C\noccupancy  ${fmt(occ, occ < 1 ? 2 : 0)} people`;
  }

  private renderPips(c: Cell, ppl: number) {
    const cap = 8;
    let html = "";
    for (let i = 0; i < cap; i++) html += `<i class="${i < ppl ? "on" : ""}"></i>`;
    if (ppl > cap) html += `<i class="more">+</i>`;
    c.pips.innerHTML = html;
  }

  alert(a: Alert, zoneByIdx: Record<number, string>) {
    this.alertCount++;
    $("acount").textContent = String(this.alertCount);
    const zoneName = a.zoneIdx != null ? zoneByIdx[a.zoneIdx] : undefined;
    if (zoneName) {
      this.twin.flashZone(zoneName);
      this.cells.get(zoneName)?.root.classList.add("alarm");
      setTimeout(() => this.cells.get(zoneName)?.root.classList.remove("alarm"), 4000);
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
    // occupancy is a live-only signal; fade the pips + hide the colour-by control in replay
    document.getElementById("zonematrix")!.classList.toggle("replay", mode === "history");
    document.getElementById("viewmode")!.classList.toggle("hidden", mode === "history");
  }

  historyFrame(f: HFrame, total: number) {
    const d = stepDate(f.step);
    this.twin.setTimeOfDay(d.hour);
    this.setPhase(d.hour);
    this.twin.setRaining(false);   // rain isn't stored in history
    document.getElementById("stage")!.classList.remove("raining");
    $("wx-sky").innerHTML = `<span class="wx-sky-ico">·</span> —`;
    const ds = `${MONTHS[d.month]} ${d.day} · ${String(d.hour).padStart(2, "0")}:00`;
    $("clock").textContent = `${ds} · replay`;
    $("sim-date").textContent = ds.replace(" · ", "  ");
    $("sim-step").textContent = `${f.step.toLocaleString()} / 35,040`;
    $("tp-frame").textContent = `${MONTHS[d.month]} ${d.day} ${String(d.hour).padStart(2, "0")}:00 · ${f.i + 1}/${total}`;
    const unit = f.metric === "temp" ? "°C" : "°C set";
    const vals: number[] = [];
    for (const [zone, v] of Object.entries(f.values)) {
      if (v == null) continue;
      vals.push(v);
      this.twin.setZoneTemp(zone, v);
      const c = this.cells.get(zone);
      if (c) {
        const col = cssColor(tempColor(v));
        c.temp.textContent = `${v.toFixed(1)}°`;
        c.temp.style.color = col;
        c.root.style.setProperty("--tc", col);
      }
    }
    const mean = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : NaN;
    $("meanzone").textContent = `${fmt(mean)} ${unit}`;
  }

  private buildZoneMatrix() {
    const root = $("zonematrix");
    const occ = Object.entries(this.geo.zones).filter(([, z]) => z.occupied);
    $("zcount").textContent = `${occ.length}`;
    for (const floor of this.geo.floors) {
      const zonesHere = occ.filter(([, z]) => z.floor === floor).map(([n]) => n).sort(zoneSort);
      if (!zonesHere.length) continue;
      const sec = document.createElement("div"); sec.className = "zfloor";
      const head = document.createElement("div"); head.className = "zfloor-h";
      head.innerHTML = `<span>${FLOOR_LABEL[floor] ?? floor.toUpperCase()}</span>` +
        `<span class="zfloor-n">${zonesHere.length}</span>`;
      const grid = document.createElement("div"); grid.className = "zfloor-grid";
      for (const name of zonesHere) {
        const cell = document.createElement("div"); cell.className = "zcell";
        const nm = document.createElement("span"); nm.className = "zc-name"; nm.textContent = zoneAbbr(name);
        const tp = document.createElement("span"); tp.className = "zc-temp"; tp.textContent = "—";
        const pp = document.createElement("div"); pp.className = "zc-pips";
        cell.append(nm, tp, pp);
        // hover → highlight the zone in 3D; click → glide camera to it (toggle)
        cell.addEventListener("mouseenter", () => this.twin.highlightZone(name, true));
        cell.addEventListener("mouseleave", () => this.twin.highlightZone(name, false));
        cell.addEventListener("click", () => {
          const on = this.twin.focusZone(name);
          for (const c of this.cells.values()) c.root.classList.remove("sel");
          if (on) cell.classList.add("sel");
        });
        grid.appendChild(cell);
        this.cells.set(name, { root: cell, name: nm, temp: tp, pips: pp, lastOcc: -1 });
      }
      sec.append(head, grid);
      root.appendChild(sec);
    }
  }

  private floorBtns = new Map<string, HTMLButtonElement>();

  /** Set a floor's visibility + keep its toggle button in sync (used by the plan lens too). */
  private setFloorOn(floor: string, on: boolean) {
    const b = this.floorBtns.get(floor); if (!b) return;
    b.dataset.on = on ? "1" : "";
    b.classList.toggle("off", !on);
    this.twin.setFloorVisible(floor, on);
  }

  private buildFloorToggles() {
    const box = $("floors");
    for (const floor of this.geo.floors) {
      const b = document.createElement("button");
      b.textContent = FLOOR_LABEL[floor] ?? floor;
      b.dataset.on = "1";
      b.onclick = () => this.setFloorOn(floor, b.dataset.on !== "1");
      this.floorBtns.set(floor, b);
      box.appendChild(b);
    }

    // top-down floor-plan lens. Floors stack directly above each other, so from straight
    // overhead they overlap — entering plan view isolates the top floor; the floor toggles
    // still page through the others. Leaving it restores the full 3D model.
    const plan = document.createElement("button");
    plan.className = "plan-toggle";
    plan.textContent = "⊞ plan";
    let on = false;
    plan.onclick = () => {
      on = !on;
      plan.classList.toggle("on", on);
      plan.textContent = on ? "◇ 3D" : "⊞ plan";
      this.twin.setPlanView(on);
      const top = this.geo.floors[this.geo.floors.length - 1];
      for (const f of this.geo.floors) this.setFloorOn(f, on ? f === top : true);
    };
    box.appendChild(plan);

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
