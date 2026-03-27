"""
NautilusAgent — SSH & rsync file-transfer bridge.

Named after the nautilus: a spiral protective shell (SSH = Secure Shell) and
the Jules Verne submarine that autonomously traverses unreachable depths.

Commands (plain text to agent, prefix @nautilus-agent stripped):
  ping <user@host>                   test SSH connectivity
  exec <user@host> <cmd [args...]>   run a remote command
  sync <[user@host:]src> <dst>       rsync pull from remote
  push <src> <[user@host:]dst>       rsync push to remote
  help                               show this table

Security: arguments are NEVER passed through a shell — every token is a
discrete asyncio.create_subprocess_exec argument, preventing shell injection.

Configuration via environment variables:
  NAUTILUS_SSH_KEY           path to SSH private key (optional)
  NAUTILUS_STRICT_HOST_KEYS  "1" or "true" → enforce strict host checking
                             default: accept-new (auto-accept first connection)
"""

import asyncio
import logging
import os
import time

from ..config import CONFIG

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 10
_EXEC_TIMEOUT    = 120
_MAX_RSYNC_LINES = 20

_HELP_TEXT = """\
**NautilusAgent** — SSH & rsync bridge

| Command | Description |
|---------|-------------|
| `ping <user@host>` | Test SSH connectivity |
| `exec <user@host> <cmd [args...]>` | Run remote command |
| `sync <[user@host:]src> <dst>` | rsync pull |
| `push <src> <[user@host:]dst>` | rsync push |
| `help` | Show this message |"""


def _ssh_opts(strict: bool = False) -> list[str]:
    opts = ["-o", f"ConnectTimeout={_CONNECT_TIMEOUT}"]
    if not strict:
        opts += ["-o", "StrictHostKeyChecking=accept-new"]
    return opts


class NautilusAgent(Actor):
    """SSH & rsync file-transfer bridge actor."""

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "nautilus-agent")
        super().__init__(**kwargs)
        self.protected = False
        self._ssh_key    = CONFIG.nautilus_ssh_key
        strict_env       = CONFIG.nautilus_strict_host_keys
        self._strict     = strict_env in ("1", "true")

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {
                "agentId":   self.actor_id,
                "agentName": self.name,
                "agentType": "transfer",
                "timestamp": time.time(),
            },
        )
        logger.info(
            "[%s] NautilusAgent started — ssh_key=%s, strict=%s",
            self.name, self._ssh_key, self._strict,
        )

    # ── handle_message ─────────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        payload = msg.payload or {}
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("content") or "")
        else:
            text = str(payload)
        text = text.strip()
        if not text:
            return
        await self._dispatch(text)

    # ── Reply helper ───────────────────────────────────────────────────────

    async def _reply(self, content: str):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/chat",
            {"from": self.name, "to": "user", "content": content, "timestamp": time.time()},
        )

    # ── SSH helpers ────────────────────────────────────────────────────────

    def _build_ssh_args(self) -> list[str]:
        args = ["ssh"] + _ssh_opts(self._strict)
        if self._ssh_key:
            args += ["-i", self._ssh_key]
        return args

    async def _run_proc(self, *args: str, timeout: int = _EXEC_TIMEOUT) -> tuple[int, str, str]:
        """Run a subprocess, return (returncode, stdout, stderr)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return (proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace"))
        except asyncio.TimeoutError:
            return (-1, "", f"Timed out after {timeout}s")
        except FileNotFoundError as exc:
            return (-1, "", str(exc))

    # ── Command handlers ───────────────────────────────────────────────────

    async def _cmd_ping(self, host: str):
        if not host:
            await self._reply("Usage: `ping <user@host>`")
            return
        await self._reply(f"Pinging `{host}`...")
        code, _, stderr = await self._run_proc(
            *self._build_ssh_args(), host, "exit",
            timeout=_CONNECT_TIMEOUT + 2,
        )
        if code == 0:
            await self._reply(f"✓ `{host}` is reachable via SSH.")
        elif code == -1:
            await self._reply(f"✗ Connection to `{host}` timed out.")
        else:
            await self._reply(
                f"✗ SSH to `{host}` failed (exit {code}):\n```\n{stderr.strip()}\n```"
            )

    async def _cmd_exec(self, host: str, remote_args: list[str]):
        if not host or not remote_args:
            await self._reply("Usage: `exec <user@host> <command [args...]>`")
            return
        display = " ".join(remote_args)
        await self._reply(f"Running `{display}` on `{host}`...")
        code, stdout, stderr = await self._run_proc(
            *self._build_ssh_args(), host, *remote_args,
        )
        icon = "✓" if code == 0 else "✗"
        parts = [f"{icon} `{display}` on `{host}` (exit {code})"]
        if stdout.strip():
            parts.append(f"```\n{stdout.strip()}\n```")
        if stderr.strip():
            parts.append(f"stderr:\n```\n{stderr.strip()}\n```")
        await self._reply("\n".join(parts))

    async def _rsync(self, src: str, dst: str, direction: str):
        await self._reply(f"Starting rsync {direction}: `{src}` → `{dst}`...")
        ssh_parts = ["ssh"] + _ssh_opts(self._strict)
        if self._ssh_key:
            ssh_parts += ["-i", self._ssh_key]
        ssh_e = " ".join(ssh_parts)

        code, stdout, stderr = await self._run_proc(
            "rsync", "-avz", "--progress", "-e", ssh_e, src, dst,
        )
        icon = "✓" if code == 0 else "✗"
        parts = [f"{icon} rsync {direction} `{src}` → `{dst}` (exit {code})"]
        if stdout.strip():
            lines = stdout.strip().splitlines()
            if len(lines) > _MAX_RSYNC_LINES:
                omit = len(lines) - _MAX_RSYNC_LINES
                tail = "\n".join(lines[-_MAX_RSYNC_LINES:])
                parts.append(f"```\n... ({omit} lines omitted) ...\n{tail}\n```")
            else:
                parts.append(f"```\n{stdout.strip()}\n```")
        if stderr.strip():
            parts.append(f"stderr:\n```\n{stderr.strip()}\n```")
        await self._reply("\n".join(parts))

    # ── Dispatcher ─────────────────────────────────────────────────────────

    async def _dispatch(self, text: str):
        # Strip prefix
        for prefix in ("@nautilus-agent", "@nautilus_agent"):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].lstrip()
                break

        tokens = text.split()
        if not tokens:
            await self._reply("Empty command. Type `help` for usage.")
            return

        cmd = tokens[0].lower()

        if cmd in ("help", "?"):
            await self._reply(_HELP_TEXT)
        elif cmd == "ping" and len(tokens) >= 2:
            await self._cmd_ping(tokens[1])
        elif cmd == "exec" and len(tokens) >= 3:
            await self._cmd_exec(tokens[1], tokens[2:])
        elif cmd == "sync" and len(tokens) >= 3:
            await self._rsync(tokens[1], tokens[2], "sync")
        elif cmd == "push" and len(tokens) >= 3:
            await self._rsync(tokens[1], tokens[2], "push")
        else:
            await self._reply(f"Unknown or incomplete command: `{cmd}`. Type `help` for usage.")

    def _current_task_description(self) -> str:
        return "SSH/rsync bridge — idle"
