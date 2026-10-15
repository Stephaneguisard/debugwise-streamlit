import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402

req = sys.stdin.read()
real_stdout = sys.stdout
reply = engine.dw_dispatch(req)
real_stdout.write("\n" + reply + "\n")
