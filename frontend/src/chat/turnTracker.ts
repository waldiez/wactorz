/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Whether a turn is under way, who it waits on, and what waits for it.
 *
 * One turn at a time: a message sent goes out, and the turn waits on whoever
 * answers it until that agent's reply ends. A message sent meanwhile is queued
 * and goes when the turn ends; so is one sent while the socket to the server is
 * closed, and one that failed to go out because it was, which go when it opens
 * again. Nothing is refused for being early. The socket is what decides, not the
 * server's link to the broker: without the broker, main still answers over it.
 *
 * A turn belongs to the agent that answers it, by id, so a message from anyone
 * else — another agent streaming at the same time, a bystander's note, another
 * agent of the same name — neither ends it nor keeps it alive. A command is
 * answered by main whichever thread it was typed in, so its turn waits on main.
 * The server says which agent a message reached before any of its reply, and
 * the turn then waits on that one. A turn that goes quiet for
 * `TURN_IDLE_TIMEOUT_MS` is given up on: none of its endings arrive if the
 * backend dies part way.
 *
 * No DOM: the chat delivers what this says to send, and the views draw from
 * what it reports.
 */
/** How long a turn may go without a sign of life before it is given up on. */
export const TURN_IDLE_TIMEOUT_MS = 60_000;

/** One message to send: the id of its bubble, its words, the thread it is sent from
 *  (`target`, an agent id), the name of the agent it is to, the id of the agent
 *  expected to answer it, and what is attached. */
export interface PendingSend {
    id: string;
    content: string;
    target: string;
    name: string;
    answerer: string;
    attachments: string[];
}

/** What the turn is now. */
export interface TurnState {
    /** The id of the agent the turn waits on, or null when none is under way. */
    agent: string | null;
    /** How many sends wait: for the turn to end, or for the connection. */
    queued: number;
}

/** What the tracker needs from the chat. */
export interface TurnHost {
    /** Put `send` on the wire now. */
    deliver(send: PendingSend): void;
    /** The turn or the queue changed. */
    changed(state: TurnState): void;
}

/** The id of who answers `content` sent to the agent `target`: main (`mainId`) for a command, else `target`. */
export function answererOf(content: string, target: string, mainId: string): string {
    return content.startsWith("/") ? mainId : target;
}

export class TurnTracker {
    private _agent: string | null = null;
    /** The send that started the turn under way, put back first if it fails. */
    private _inFlight: PendingSend | null = null;
    private _queue: PendingSend[] = [];
    /** Until told otherwise the socket is taken to be open, as the composer has
     *  always taken it: a send in the first instant goes out. */
    private _live = true;
    private _timer: ReturnType<typeof setTimeout> | undefined;

    constructor(
        private readonly host: TurnHost,
        private readonly timeoutMs = TURN_IDLE_TIMEOUT_MS,
    ) {}

    /** The turn and the queue, as they stand. */
    get state(): TurnState {
        return { agent: this._agent, queued: this._queue.length };
    }

    /** Whether a send made now goes out at once rather than waiting. */
    sendsNow(): boolean {
        return this._agent === null && this._live && this._queue.length === 0;
    }

    /** Send `send` now when nothing is under way and the connection is up; else queue it. */
    request(send: PendingSend): void {
        if (this.sendsNow()) {
            this._start(send);
            return;
        }
        this._queue.push(send);
        this._notify();
    }

    /** A piece of the reply of the agent `from` (an id) arrived: the turn it answers is alive. */
    chunk(from: string): void {
        if (this._agent !== null && from === this._agent) {
            this._arm();
        }
    }

    /** The server says the send `id` reached the agent `agentId`: if it is the
     *  send under way, its turn waits on that agent from now. */
    reached(id: string, agentId: string): void {
        if (this._inFlight?.id === id && agentId && this._agent !== agentId) {
            this._agent = agentId;
            this._arm();
            this._notify();
        }
    }

    /** The reply of the agent `from` (an id) ended, streamed or in one message: its turn is over. */
    ended(from: string): void {
        if (this._agent !== null && from === this._agent) {
            this._finish();
        }
    }

    /**
     * The send under way did not go out: the socket was closed. It goes back to
     * the front of the queue, to go first once the socket opens again, and the
     * turn ends, since no reply is coming. The send put back, or null when no
     * send of this tracker's was under way.
     */
    failed(): PendingSend | null {
        const back = this._inFlight;
        if (back) {
            this._queue.unshift(back);
        }
        this._live = false;
        this._finish(false);
        return back;
    }

    /** The socket to the server opened or closed. Closed ends the turn, since no
     *  reply is coming; open sends what was queued, one turn at a time. */
    connection(live: boolean): void {
        this._live = live;
        if (live) {
            this._next();
            return;
        }
        this._finish(false);
    }

    /** Drop the queue and end the turn: everything was wiped. */
    clear(): void {
        this._queue = [];
        this._finish(false);
    }

    private _start(send: PendingSend): void {
        this._inFlight = send;
        this._agent = send.answerer;
        this._arm();
        this._notify();
        this.host.deliver(send);
    }

    /** End the turn, if there is one, and send the next queued unless told not to. */
    private _finish(next = true): void {
        clearTimeout(this._timer);
        const was = this._agent;
        this._agent = null;
        this._inFlight = null;
        if (was !== null) {
            this._notify();
        }
        if (next) {
            this._next();
        }
    }

    private _next(): void {
        if (this._agent !== null || !this._live) {
            return;
        }
        const send = this._queue.shift();
        if (send) {
            this._start(send);
        }
    }

    private _arm(): void {
        clearTimeout(this._timer);
        this._timer = setTimeout(() => this._finish(), this.timeoutMs);
    }

    private _notify(): void {
        this.host.changed(this.state);
    }
}
