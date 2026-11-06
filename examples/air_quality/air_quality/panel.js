/**
 * SPDX-License-Identifier: Apache-2.0
 */
// The Air tab: a card per room with its latest readings, and its score over the
// simulated day, with the threshold, each episode shaded and each answer marked.
// Live only: it shows what arrives while it is open.
export const apiVersion = 1;

const ROOMS = { "meeting-room": "Meeting room", "open-office": "Open office", kitchen: "Kitchen" };
const QUANTITIES = [
    ["co2", "CO₂", "ppm", 0],
    ["temperature", "Temp", "°C", 1],
    ["humidity", "RH", "%", 0],
    ["pm25", "PM2.5", "µg/m³", 0],
];
const KEEP = 400;
const STYLE = `
.aq-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; margin: 0 0 12px; }
.aq-head h2 { margin: 0; font-size: 1.25rem; }
.aq-dim { color: var(--af-text-dim, #888); font-size: 0.85rem; }
.aq-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr)); gap: 16px; }
.aq-card { border: 1px solid color-mix(in srgb, currentColor 14%, transparent); border-radius: 10px; padding: 12px 14px; min-width: 0; }
.aq-top { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.aq-top h3 { margin: 0; font-size: 1.05rem; }
.aq-badge { border-radius: 999px; padding: 2px 10px; font-size: 0.8rem; font-weight: 600; color: #fff; background: #2e9d5b; }
.aq-badge[data-state="alert"] { background: #d64545; }
.aq-values { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2px 16px; margin-bottom: 8px; }
.aq-note { min-height: 1.2em; margin-top: 6px; }
.aq-note a { color: inherit; }
`;

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function clock(ts) {
    return new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
}

export function mount(root, host) {
    const style = el("style");
    style.textContent = STYLE;
    const head = el("div", "aq-head");
    const now = el("span", "aq-dim", "Waiting for readings: run python -m air_quality.publish");
    head.append(el("h2", "", "Air quality"), now);
    const grid = el("div", "aq-grid");
    root.append(style, head, grid);

    const cards = new Map();
    const timers = new Set();

    function card(room) {
        let c = cards.get(room);
        if (c) return c;
        const box = el("section", "aq-card");
        box.setAttribute("aria-label", ROOMS[room] ?? room);
        const top = el("div", "aq-top");
        const badge = el("span", "aq-badge", "ok");
        top.append(el("h3", "", ROOMS[room] ?? room), badge);
        const values = el("div", "aq-values");
        const cells = {};
        for (const [key, name] of QUANTITIES) {
            cells[key] = el("span", "", `${name} –`);
            values.append(cells[key]);
        }
        const note = el("div", "aq-note aq-dim");
        box.append(top, values);
        c = { box, badge, cells, note, points: [], threshold: null, bands: [], marks: [], chart: null, due: false };
        if (typeof host.chart === "function") {
            c.chart = host.chart([], { title: `${ROOMS[room] ?? room}: score`, yLabel: "spreads from usual", mode: "line" });
            box.append(c.chart.element);
        }
        box.append(note);
        // Rooms in the order the building lists them, whichever reports first.
        const order = Object.keys(ROOMS);
        const after = [...cards.entries()].find(([other]) => order.indexOf(other) > order.indexOf(room));
        grid.insertBefore(box, after ? after[1].box : null);
        cards.set(room, c);
        return c;
    }

    function redraw(c) {
        if (!c.chart || c.due) return;
        c.due = true;
        const timer = setTimeout(() => {
            timers.delete(timer);
            c.due = false;
            const options = {
                thresholds: c.threshold === null ? [] : [{ value: c.threshold, label: "alarm" }],
                bands: c.bands,
                marks: c.marks,
            };
            c.chart.update([{ name: "score", points: c.points }], options);
        }, 400);
        timers.add(timer);
    }

    const stops = [
        host.subscribe("sensors/air/+", (_topic, reading) => {
            if (!reading || !ROOMS[reading.device]) return;
            const c = card(reading.device);
            for (const [key, name, unit, digits] of QUANTITIES) {
                const value = Number(reading[key]);
                c.cells[key].textContent = Number.isFinite(value) ? `${name} ${value.toFixed(digits)} ${unit}` : `${name} –`;
            }
            now.textContent = `Tuesday ${clock(reading.ts)} UTC (simulated)`;
        }),
        host.subscribe("air/+/score", (_topic, scored) => {
            if (!scored || !ROOMS[scored.device]) return;
            const c = card(scored.device);
            c.threshold = Number(scored.threshold);
            c.points.push({ t: scored.ts * 1000, v: Number(scored.score) });
            if (c.points.length > KEEP) c.points.splice(0, c.points.length - KEEP);
            redraw(c);
        }),
        host.subscribe("air/episodes", (_topic, episode) => {
            if (!episode || !ROOMS[episode.device]) return;
            const c = card(episode.device);
            if (episode.state === "open") {
                c.badge.textContent = "alert";
                c.badge.dataset.state = "alert";
                c.note.textContent = `Since ${clock(episode.start)}: ${episode.worst} far from usual.`;
            } else if (episode.state === "closed") {
                c.badge.textContent = "ok";
                delete c.badge.dataset.state;
                c.bands.push({ from: episode.start * 1000, to: episode.end * 1000, label: episode.worst, tone: "warn" });
                c.note.replaceChildren(`${clock(episode.start)}–${clock(episode.end)}: asked what it was. `);
                const link = el("a", "", "Answer in the Inbox →");
                link.href = "#/inbox";
                c.note.append(link);
            } else if (episode.state === "answered") {
                c.marks.push({ t: episode.end * 1000, label: `answered: ${episode.answer}`, shape: "diamond" });
                c.note.textContent = `${clock(episode.start)}–${clock(episode.end)}: ${episode.answer}.`;
            }
            redraw(c);
        }),
    ];

    return () => {
        for (const stop of stops) stop();
        for (const timer of timers) clearTimeout(timer);
        for (const c of cards.values()) c.chart?.destroy();
    };
}
