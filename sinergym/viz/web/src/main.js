// Bootstrap: load geometry → build twin → connect live feed → wire dashboard,
// with a history-replay transport that scrubs a finished run from Fuseki.
import { Feed } from "./data";
import { HistoryPlayer } from "./history";
import { Twin } from "./twin";
import { UI } from "./ui";
const $ = (id) => document.getElementById(id);
async function boot() {
    const geo = await fetch("/api/geometry").then((r) => r.json());
    const canvas = document.getElementById("twin");
    const twin = new Twin(canvas, geo);
    const ui = new UI(geo, twin);
    let mode = "live";
    // ── live feed ──────────────────────────────────────────────────────────────
    const feed = new Feed();
    feed.onStatus((ok) => ui.setConn(ok));
    feed.onFrame((f) => { if (mode === "live")
        ui.frame(f); });
    feed.onAlert((a) => { if (mode === "live")
        ui.alert(a, feed.zoneByIdx); });
    feed.connect();
    // ── history replay ───────────────────────────────────────────────────────────
    const player = new HistoryPlayer();
    player.onFrame = (f) => {
        ui.historyFrame(f, player.length);
        $("tp-slider").value = String(f.i);
    };
    const show = (el, on = true) => el.classList.toggle("hidden", !on);
    const playBtn = $("tp-play"), slider = $("tp-slider");
    $("tp-load").addEventListener("click", async () => {
        $("tp-load").textContent = "loading…";
        try {
            const h = await player.load(720);
            mode = "history";
            ui.setMode("history");
            slider.max = String(h.steps.length - 1);
            slider.value = "0";
            show($("tp-load"), false);
            [playBtn, slider, $("tp-frame"), $("tp-metric"), $("tp-live")].forEach((e) => show(e));
            playBtn.textContent = "▶";
        }
        catch (e) {
            $("tp-load").textContent = "⟲ replay run";
            console.error(e);
        }
    });
    playBtn.addEventListener("click", () => {
        if (player.playing) {
            player.pause();
            playBtn.textContent = "▶";
        }
        else {
            player.play();
            playBtn.textContent = "❚❚";
        }
    });
    slider.addEventListener("input", () => { player.pause(); playBtn.textContent = "▶"; player.seek(+slider.value); });
    $("tp-metric").addEventListener("change", (e) => player.setMetric(e.target.value));
    $("tp-live").addEventListener("click", () => {
        player.pause();
        mode = "live";
        ui.setMode("live");
        show($("tp-load"));
        $("tp-load").textContent = "⟲ replay run";
        [playBtn, slider, $("tp-frame"), $("tp-metric"), $("tp-live")].forEach((e) => show(e, false));
    });
}
boot().catch((e) => {
    console.error(e);
    document.body.insertAdjacentHTML("beforeend", `<div style="position:fixed;inset:0;display:grid;place-items:center;color:#ff5470;
      font:14px sans-serif;background:#070b14">Failed to load: ${e}</div>`);
});
