export class HistoryPlayer {
    h;
    i = 0;
    timer;
    metric = "clg";
    fps = 24;
    onFrame;
    get loaded() { return !!this.h; }
    get length() { return this.h?.steps.length ?? 0; }
    get index() { return this.i; }
    get data() { return this.h; }
    async load(frames = 720) {
        const h = await fetch(`/api/history?frames=${frames}`).then((r) => r.json());
        this.h = h;
        this.i = 0;
        this.emit();
        return h;
    }
    setMetric(m) { this.metric = m; this.emit(); }
    seek(i) {
        if (!this.h)
            return;
        this.i = Math.max(0, Math.min(this.h.steps.length - 1, i));
        this.emit();
    }
    play() {
        if (!this.h || this.timer)
            return;
        this.timer = window.setInterval(() => {
            if (!this.h)
                return;
            this.i++;
            if (this.i >= this.h.steps.length) {
                this.i = 0;
            }
            this.emit();
        }, 1000 / this.fps);
    }
    pause() { if (this.timer) {
        clearInterval(this.timer);
        this.timer = undefined;
    } }
    get playing() { return !!this.timer; }
    emit() {
        if (!this.h || !this.onFrame)
            return;
        const src = this.h[this.metric];
        const values = {};
        for (const z of this.h.zones)
            values[z] = src[z]?.[this.i] ?? null;
        this.onFrame({ i: this.i, step: this.h.steps[this.i], values, metric: this.metric });
    }
}
