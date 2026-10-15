"""Run engine requests (student code!) in a separate, resource-limited Python process.

The Streamlit server never exec()s student code in its own process: each request is a
fresh `python -I runner` subprocess with a timeout, CPU/memory limits and an empty environment.
This is a demo-grade guard, not a hardened sandbox (see README).
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMEOUT = 7


def _limits():  # runs in the child before exec (POSIX only)
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT, TIMEOUT))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 2 ** 20, 512 * 2 ** 20))
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 ** 20, 2 ** 20))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except Exception:
        pass


def dispatch(payload: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            p = subprocess.run(
                [sys.executable, "-I", str(HERE / "runner.py")],
                input=json.dumps(payload), capture_output=True, text=True, timeout=TIMEOUT + 2,
                cwd=tmp, env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"},
                preexec_fn=_limits if sys.platform != "win32" else None)
        except subprocess.TimeoutExpired:
            return {"timeout": True, "limit": TIMEOUT}
    if p.returncode != 0 or not p.stdout.strip():
        if p.returncode in (-24, -9, -11):
            return {"timeout": True, "limit": TIMEOUT}
        return {"internal": True, "error": (p.stderr or "runner failed").strip()[-400:]}
    try:
        # student prints go to the captured buffer; the last line is always the engine reply
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"internal": True, "error": "unreadable runner output"}
