/**
 * SPDX-License-Identifier: Apache-2.0
 */
// The Energy tab: the meter over the simulated day with a mark at each run's
// start, saying whether its guess was right, each wrong guess shaded and named,
// and a list of the runs with each answer beside its guess. Runs are many and
// short, so the chart names only the wrong ones and the list names them all.
// Live only: it shows what arrives while it is open.
export const apiVersion = 1;

const TITLES = { kettle: "Kettle", microwave: "Microwave", washer: "Washing machine", oven: "Oven", other: "Something else" };
const KEEP = 1500;
const STYLE = `
.he-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; margin: 0 0 12px; }
.he-head h2 { margin: 0; font-size: 1.25rem; }
.he-dim { color: var(--af-text-dim, #888); font-size: 0.85rem; }
.he-card { border: 1px solid color-mix(in srgb, currentColor 14%, transparent); border-radius: 10px; padding: 12px 14px; min-width: 0; }
.he-top { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; margin-bottom: 8px; }
.he-now { font-size: 1.6rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.he-runs { list-style: none; margin: 12px 0 0; padding: 0; display: grid; gap: 6px; }
.he-run { display: flex; flex-wrap: wrap; gap: 2px 12px; padding: 6px 0; border-top: 1px solid color-mix(in srgb, currentColor 10%, transparent); }
.he-run > span:first-child { font-variant-numeric: tabular-nums; min-width: 11em; }
.he-verdict[data-right="yes"] { color: #2e9d5b; font-weight: 600; }
.he-verdict[data-right="no"] { color: #d64545; font-weight: 600; }
.he-runs a { color: inherit; }
`;

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function clock(ts) {
    return new Date(ts * 1000).toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
        timeZone: "UTC",
    });
}

function title(appliance) {
    return TITLES[appliance] ?? String(appliance);
}

const watts = value => `${Math.round(value).toLocaleString()} W`;

// How each run is marked on the chart, by what became of its guess: by shape
// as well as colour, and counted in the legend.
const MARKS = {
    right: { label: "guessed right", shape: "dot", color: "#2e9d5b" },
    wrong: { label: "guessed wrong", shape: "triangle", color: "#d64545" },
    asked: { label: "waiting for an answer", shape: "square" },
};

function verdict(run) {
    if (!run.answer) return "asked";
    return run.answer === run.guess ? "right" : "wrong";
}

export function mount(root, host) {
    const style = el("style");
    style.textContent = STYLE;
    const head = el("div", "he-head");
    const when = el("span", "he-dim", "Waiting for readings: run python -m household_energy.publish");
    head.append(el("h2", "", "Household energy"), when);

    const card = el("section", "he-card");
    card.setAttribute("aria-label", "Power meter");
    const top = el("div", "he-top");
    const now = el("span", "he-now", "– W");
    const tally = el("span", "he-dim", "No runs yet");
    top.append(now, tally);
    card.append(top);
    const chart =
        typeof host.chart === "function"
            ? host.chart([], { title: "Power meter", yLabel: "W", mode: "line", format: watts })
            : null;
    if (chart) card.append(chart.element);
    const list = el("ul", "he-runs");
    list.setAttribute("aria-label", "Runs");
    card.append(list);
    root.append(style, head, card);

    const points = [];
    // Each run by its start, in the order they came.
    const runs = new Map();
    const timers = new Set();
    let due = false;

    function redraw() {
        if (!chart || due) return;
        due = true;
        const timer = setTimeout(() => {
            timers.delete(timer);
            due = false;
            const guessed = [...runs.values()].filter(run => run.guess);
            const wrong = guessed.filter(run => run.answer && run.answer !== run.guess);
            const bands = wrong.map(run => ({
                from: run.ts * 1000,
                to: run.end * 1000,
                label: `${title(run.guess)} → ${title(run.answer)}`,
                tone: "warn",
            }));
            const marks = guessed.map(run => ({ t: run.ts * 1000, v: run.watts, ...MARKS[verdict(run)] }));
            chart.update([{ name: "meter", points }], { bands, marks });
        }, 300);
        timers.add(timer);
    }

    function retally() {
        const all = [...runs.values()];
        const answered = all.filter(run => run.answer);
        const right = answered.filter(run => run.answer === run.guess).length;
        tally.textContent = all.length
            ? `Runs ${all.length} · answered ${answered.length} · guessed right ${right} · wrong ${answered.length - right}`
            : "No runs yet";
    }

    function row(run) {
        if (!run.row) {
            run.row = el("li", "he-run");
            run.row.setAttribute("role", "listitem");
            list.append(run.row);
        }
        const verdict = el("span", "he-verdict");
        if (run.answer) {
            const right = run.answer === run.guess;
            verdict.dataset.right = right ? "yes" : "no";
            verdict.textContent = right ? `✓ ${title(run.answer)}` : `✗ it was ${title(run.answer)}`;
        } else if (run.guess) {
            const link = el("a", "", "Answer in the Inbox →");
            link.href = "#/inbox";
            verdict.append(link);
        }
        run.row.replaceChildren(
            el("span", "", `${clock(run.ts)}–${clock(run.end)} · ${watts(run.watts)} · ${run.minutes} min`),
            el("span", "he-dim", run.guess ? `guess: ${title(run.guess)}` : "guessing…"),
            verdict,
        );
        retally();
        redraw();
    }

    function runFor(message) {
        let run = runs.get(message.ts);
        if (!run) {
            run = { ts: message.ts, end: message.end, watts: message.watts, minutes: message.minutes };
            runs.set(message.ts, run);
        }
        return run;
    }

    const stops = [
        host.subscribe("sensors/energy/meter", (_topic, reading) => {
            if (!reading || !Number.isFinite(Number(reading.watts))) return;
            now.textContent = watts(Number(reading.watts));
            when.textContent = `Saturday ${clock(reading.ts)} UTC (simulated)`;
            points.push({ t: reading.ts * 1000, v: Number(reading.watts) });
            if (points.length > KEEP) points.splice(0, points.length - KEEP);
            redraw();
        }),
        host.subscribe("energy/runs", (_topic, run) => {
            if (run && Number.isFinite(Number(run.ts))) row(runFor(run));
        }),
        host.subscribe("energy/guesses", (_topic, guessed) => {
            if (!guessed || !guessed.appliance) return;
            const run = runFor(guessed);
            run.guess = guessed.appliance;
            row(run);
        }),
        host.subscribe("energy/answers", (_topic, answered) => {
            if (!answered || !answered.answer) return;
            const run = runFor(answered);
            run.guess = run.guess ?? answered.guess;
            run.answer = answered.answer;
            row(run);
        }),
    ];

    return () => {
        for (const stop of stops) stop();
        for (const timer of timers) clearTimeout(timer);
        chart?.destroy();
    };
}
