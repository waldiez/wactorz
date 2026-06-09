// Bootstrap: load geometry → build twin → connect live feed → wire dashboard,
// with a history-replay transport that scrubs a finished run from Fuseki.
import { AskPanel } from "./ask";
import { Feed, type Geometry } from "./data";
import { HistoryPlayer, type Metric } from "./history";
import { Twin } from "./twin";
import { UI } from "./ui";

const $ = (id: string) => document.getElementById(id) as HTMLElement;

interface VizConfig { mqttWsPort: number; wsChatPort: number; building: string }

async function boot() {
  const [geo, cfg] = await Promise.all([
    fetch("/api/geometry").then((r) => r.json()) as Promise<Geometry>,
    fetch("/api/config").then((r) => r.json()).catch(() => ({ mqttWsPort: 9001, wsChatPort: 8888 })) as Promise<VizConfig>,
  ]);
  const canvas = document.getElementById("twin") as HTMLCanvasElement;
  const twin = new Twin(canvas, geo);
  const ui = new UI(geo, twin);

  let mode: "live" | "history" = "live";

  // ── live feed ──────────────────────────────────────────────────────────────
  const feed = new Feed();
  feed.onStatus((ok) => ui.setConn(ok));
  feed.onFrame((f) => { if (mode === "live") ui.frame(f); });
  feed.onAlert((a) => { if (mode === "live") ui.alert(a, feed.zoneByIdx); });
  feed.connect(cfg.mqttWsPort);

  // ── ask-the-run (hsml Q&A over wactorz monitor WS) ──────────────────────────
  new AskPanel(cfg.wsChatPort);

  // ── history replay ───────────────────────────────────────────────────────────
  const player = new HistoryPlayer();
  player.onFrame = (f) => {
    ui.historyFrame(f, player.length);
    ($("tp-slider") as HTMLInputElement).value = String(f.i);
  };

  const show = (el: HTMLElement, on = true) => el.classList.toggle("hidden", !on);
  const playBtn = $("tp-play"), slider = $("tp-slider") as HTMLInputElement;

  $("tp-load").addEventListener("click", async () => {
    $("tp-load").textContent = "loading…";
    try {
      const h = await player.load(720);
      mode = "history"; ui.setMode("history");
      slider.max = String(h.steps.length - 1); slider.value = "0";
      show($("tp-load"), false);
      [playBtn, slider, $("tp-frame"), $("tp-metric"), $("tp-live")].forEach((e) => show(e));
      playBtn.textContent = "▶";
    } catch (e) {
      $("tp-load").textContent = "⟲ replay run";
      console.error(e);
    }
  });

  playBtn.addEventListener("click", () => {
    if (player.playing) { player.pause(); playBtn.textContent = "▶"; }
    else { player.play(); playBtn.textContent = "❚❚"; }
  });
  slider.addEventListener("input", () => { player.pause(); playBtn.textContent = "▶"; player.seek(+slider.value); });
  $("tp-metric").addEventListener("change", (e) =>
    player.setMetric((e.target as HTMLSelectElement).value as Metric));

  $("tp-live").addEventListener("click", () => {
    player.pause();
    mode = "live"; ui.setMode("live");
    show($("tp-load")); $("tp-load").textContent = "⟲ replay run";
    [playBtn, slider, $("tp-frame"), $("tp-metric"), $("tp-live")].forEach((e) => show(e, false));
  });
}

boot().catch((e) => {
  console.error(e);
  document.body.insertAdjacentHTML(
    "beforeend",
    `<div style="position:fixed;inset:0;display:grid;place-items:center;color:#ff5470;
      font:14px sans-serif;background:#070b14">Failed to load: ${e}</div>`,
  );
});
