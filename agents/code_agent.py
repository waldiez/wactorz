"""
CodeAgent - An actor that can write AND execute code.
Execution is sandboxed with configurable permissions.
"""

import asyncio
import logging
import sys
import tempfile
import traceback
from io import StringIO
from pathlib import Path
from typing import Optional

from ..core.actor import Actor, Message, MessageType
from .llm_agent import LLMAgent, LLMProvider

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM = """You are an expert Python developer agent.
When given a task, write clean, executable Python code to solve it.
Return ONLY the code block (no markdown fences unless asked).
Be concise. Prefer standard library when possible."""


class CodeAgent(LLMAgent):
    """
    An agent that can generate Python code via LLM and execute it.
    Supports in-process execution (fast) and subprocess execution (safe).
    """

    def __init__(
        self,
        execution_mode: str = "subprocess",  # "inprocess" | "subprocess"
        allowed_modules: Optional[list[str]] = None,
        working_dir: str = "./code_workspace",
        **kwargs,
    ):
        kwargs.setdefault("system_prompt", DEFAULT_SYSTEM)
        super().__init__(**kwargs)
        self.execution_mode = execution_mode
        self.allowed_modules = allowed_modules  # None = unrestricted
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            payload = msg.payload if isinstance(msg.payload, dict) else {"text": str(msg.payload)}
            action = payload.get("action", "generate_and_run")

            if action == "run":
                code = payload.get("code", "")
                result = await self.execute_code(code)
            elif action == "generate":
                result = {"code": await self._generate_code(payload.get("text", ""))}
            else:  # generate_and_run
                code = await self._generate_code(payload.get("text", ""))
                result = await self.execute_code(code)
                result["code"] = code

            self.metrics.tasks_completed += 1
            if msg.sender_id:
                await self.send(msg.sender_id, MessageType.RESULT, result)

    async def _generate_code(self, task: str) -> str:
        return await self.chat(task)

    async def execute_code(self, code: str) -> dict:
        """Execute code and return stdout, stderr, return_code."""
        if self.execution_mode == "inprocess":
            return await self._run_inprocess(code)
        else:
            return await self._run_subprocess(code)

    async def _run_inprocess(self, code: str) -> dict:
        """Run code in the same process. Fast but less isolated."""
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = captured_out = StringIO()
        sys.stderr = captured_err = StringIO()
        result = {"stdout": "", "stderr": "", "return_code": 0, "error": None}
        try:
            exec(compile(code, "<agent_code>", "exec"), {"__name__": "__agent__"})
        except Exception as e:
            result["error"] = traceback.format_exc()
            result["return_code"] = 1
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            result["stdout"] = captured_out.getvalue()
            result["stderr"] = captured_err.getvalue()
        return result

    async def _run_subprocess(self, code: str) -> dict:
        """Run code in a subprocess. Isolated and safer."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", dir=self.working_dir, delete=False
        ) as f:
            f.write(code)
            script_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.working_dir),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                return {"stdout": "", "stderr": "Execution timed out (30s)", "return_code": -1}

            return {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "return_code": proc.returncode,
                "script": script_path,
            }
        finally:
            Path(script_path).unlink(missing_ok=True)
