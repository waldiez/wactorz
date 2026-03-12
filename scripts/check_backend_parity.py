#!/usr/bin/env python3
import json
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "parity_fixtures" / "backend_supervisor_parity.json"


def _run(cmd: list[str], cwd: pathlib.Path) -> dict:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode)
    return json.loads(proc.stdout)


def main() -> int:
    python_result = _run(
        [sys.executable, "tests/backend_parity_harness.py", "--fixture", str(FIXTURE), "--assert-expected"],
        ROOT,
    )
    rust_result = _run(
        [
            "cargo",
            "run",
            "-q",
            "-p",
            "agentflow-core",
            "--bin",
            "backend_parity",
            "--",
            "--fixture",
            str(FIXTURE),
            "--assert-expected",
        ],
        ROOT / "rust",
    )
    if python_result != rust_result:
        print(json.dumps({"python": python_result, "rust": rust_result}, indent=2, sort_keys=True))
        return 1
    print(json.dumps(python_result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
