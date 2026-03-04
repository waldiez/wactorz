/**
 * NautilusAgent — SSH/rsync bridge (Node.js port).
 * Uses child_process.spawn for security (no shell interpolation).
 * agentType: "transfer"
 */

import { spawn } from "child_process";
import { Actor, MqttPublisher } from "../core/actor";
import { Message, MessageType } from "../core/types";

const SSH_KEY         = process.env["NAUTILUS_SSH_KEY"];
const STRICT_KEYS     = process.env["NAUTILUS_STRICT_HOST_KEYS"] === "1" ||
                        process.env["NAUTILUS_STRICT_HOST_KEYS"]?.toLowerCase() === "true";
const CONNECT_TIMEOUT = 10;
const EXEC_TIMEOUT    = 120_000; // ms

const HELP = `**NautilusAgent** — SSH & rsync bridge

| Command | Description |
|---------|-------------|
| \`ping <user@host>\` | Test SSH connectivity |
| \`exec <user@host> <cmd [args…]>\` | Run remote command |
| \`sync <[user@host:]src> <dst>\` | rsync pull |
| \`push <src> <[user@host:]dst>\` | rsync push |
| \`help\` | Show this message |`;

function sshOpts(): string[] {
  const opts: string[] = ["-o", `ConnectTimeout=${CONNECT_TIMEOUT}`];
  if (!STRICT_KEYS) opts.push("-o", "StrictHostKeyChecking=accept-new");
  if (SSH_KEY) opts.push("-i", SSH_KEY);
  return opts;
}

function runCmd(
  cmd: string,
  args: string[],
  timeoutMs: number
): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolve) => {
    const proc = spawn(cmd, args, { stdio: "pipe" });
    let stdout = "";
    let stderr = "";
    proc.stdout?.on("data", (d: Buffer) => { stdout += d.toString(); });
    proc.stderr?.on("data", (d: Buffer) => { stderr += d.toString(); });

    const timer = setTimeout(() => {
      proc.kill();
      resolve({ stdout, stderr: `Timed out after ${timeoutMs / 1000}s`, code: -1 });
    }, timeoutMs);

    proc.on("close", (code) => {
      clearTimeout(timer);
      resolve({ stdout, stderr, code: code ?? -1 });
    });
    proc.on("error", (e) => {
      clearTimeout(timer);
      resolve({ stdout, stderr: e.message, code: -1 });
    });
  });
}

export class NautilusAgent extends Actor {
  constructor(publish: MqttPublisher, actorId?: string) {
    super("nautilus-agent", publish, actorId);
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("transfer");
  }

  override async handleMessage(msg: Message): Promise<void> {
    if (msg.type !== MessageType.Task && msg.type !== MessageType.Text) return;
    let text = Actor.extractText(msg.payload).trim();
    if (!text) return;
    text = Actor.stripPrefix(text, "@nautilus-agent", "@nautilus_agent");
    await this._dispatch(text.trim());
    this.metrics.tasksCompleted++;
  }

  private async _dispatch(text: string): Promise<void> {
    const tokens = text.split(/\s+/);
    const cmd = tokens[0]?.toLowerCase() ?? "";

    switch (cmd) {
      case "":
      case "help":
        this.replyChat(HELP);
        break;
      case "ping":
        await this._cmdPing(tokens[1] ?? "");
        break;
      case "exec":
        await this._cmdExec(tokens[1] ?? "", tokens.slice(2));
        break;
      case "sync":
        await this._cmdRsync(tokens[1] ?? "", tokens[2] ?? "", "sync");
        break;
      case "push":
        await this._cmdRsync(tokens[1] ?? "", tokens[2] ?? "", "push");
        break;
      default:
        this.replyChat(`Unknown command: \`${cmd}\`. Type \`help\`.`);
    }
  }

  private async _cmdPing(host: string): Promise<void> {
    if (!host) { this.replyChat("Usage: `ping <user@host>`"); return; }
    this.replyChat(`Pinging \`${host}\`…`);
    const { code, stderr } = await runCmd("ssh", [...sshOpts(), host, "exit"], (CONNECT_TIMEOUT + 2) * 1_000);
    if (code === 0) {
      this.replyChat(`✓ \`${host}\` is reachable via SSH.`);
    } else {
      this.replyChat(`✗ SSH to \`${host}\` failed (exit ${code}):\n\`\`\`\n${stderr.trim()}\n\`\`\``);
    }
  }

  private async _cmdExec(host: string, remoteArgs: string[]): Promise<void> {
    if (!host || remoteArgs.length === 0) {
      this.replyChat("Usage: `exec <user@host> <command [args…]>`");
      return;
    }
    const displayCmd = remoteArgs.join(" ");
    this.replyChat(`Running \`${displayCmd}\` on \`${host}\`…`);
    const { stdout, stderr, code } = await runCmd("ssh", [...sshOpts(), host, ...remoteArgs], EXEC_TIMEOUT);
    const icon = code === 0 ? "✓" : "✗";
    let reply = `${icon} \`${displayCmd}\` on \`${host}\` (exit ${code})`;
    if (stdout.trim()) reply += `\n\`\`\`\n${stdout.trim()}\n\`\`\``;
    if (stderr.trim()) reply += `\nstderr:\n\`\`\`\n${stderr.trim()}\n\`\`\``;
    this.replyChat(reply);
  }

  private async _cmdRsync(src: string, dst: string, direction: string): Promise<void> {
    if (!src || !dst) {
      this.replyChat(direction === "sync"
        ? "Usage: `sync <[user@host:]src-path> <local-dst-path>`"
        : "Usage: `push <local-src-path> <[user@host:]dst-path>`"
      );
      return;
    }
    this.replyChat(`Starting rsync ${direction}: \`${src}\` → \`${dst}\`…`);
    const sshE = ["ssh", ...sshOpts()].join(" ");
    const { stdout, stderr, code } = await runCmd(
      "rsync",
      ["-avz", "--progress", "-e", sshE, src, dst],
      EXEC_TIMEOUT
    );
    const icon = code === 0 ? "✓" : "✗";
    let reply = `${icon} rsync ${direction} \`${src}\` → \`${dst}\` (exit ${code})`;
    if (stdout.trim()) {
      const lines = stdout.trim().split("\n");
      const tail  = lines.length > 20 ? `… (${lines.length - 20} lines omitted) …\n${lines.slice(-20).join("\n")}` : lines.join("\n");
      reply += `\n\`\`\`\n${tail}\n\`\`\``;
    }
    if (stderr.trim()) reply += `\nstderr:\n\`\`\`\n${stderr.trim()}\n\`\`\``;
    this.replyChat(reply);
  }
}
