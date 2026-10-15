# DebugWise AI · Streamlit edition

Feature-for-feature port of [debugwise-ai](https://github.com/Stephaneguisard/debugwise-ai) in Python/Streamlit, all in English.
Share links (`?task=…`, `?record=…`) and `.json` records use the same format as the web version, so the two are interchangeable.

## Features

**Student view**
- Pick one of 4 sample tasks, a teacher assignment (from a link), or **type/paste your own Python code** (with optional `call => expected` tests and `input()` data).
- 7-step flow: hypothesis → experiment → support ladder → revise & test → explain → transfer → reflect.
- Three-level support ladder, unlocked only after a *new* hypothesis and a *new logged experiment* since the last hint; hypothesis quality feedback; low-confidence warning.
- Built-in rule-based diagnosis (`engine.py`, same engine as the web version) **or Claude**: add your own Anthropic key in the sidebar for AI hints, follow-up questions (max 6/task, never beyond the unlocked level) and explanation feedback. If Claude fails the app falls back to the built-in engine.
- Review flags for the teacher (low confidence, rapid hints, early jump to fix, off-track fix, thin explanation).
- Learning record: share link, `.md` and `.json` download.

**Teacher view**
- Records: stats, flagged-only filter, per-student timeline (hypotheses, experiments, hints, questions), explanation, transfer, reflection, original + revised code; open records from a link, pasted JSON or uploaded files; example records included.
- Assignments: create a task from buggy code + tests (validated: it must actually fail), copy the student link, open as student, delete.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/streamlit run streamlit_app.py
.venv/bin/pytest -q
```

## Deploy free to xxx.streamlit.app

1. Go to <https://share.streamlit.io>, sign in with GitHub.
2. Create app → pick this repo, branch `main`, main file `streamlit_app.py`.
3. Choose an App URL (subdomain) and Deploy. Pushing to `main` redeploys automatically.

Optional: put `ANTHROPIC_API_KEY = "sk-ant-..."` in the app's **Secrets** to give every visitor Claude (billed to you; the sidebar status then reads “app key”). Without it, visitors can paste their own key or use the built-in diagnosis.

## Files

| File | Purpose |
|---|---|
| `streamlit_app.py` | UI: student view, teacher view |
| `core.py` | Logic ported from the web app: gating, flags, Claude prompts, records, share-link packing |
| `engine.py` | Diagnosis engine (identical to the web version) |
| `sandbox.py`, `runner.py` | Run student code in a separate process: 7 s timeout, CPU/memory/file limits, empty environment |
| `tasks.json`, `examples.json` | Sample tasks and example records (exported from the web version) |
| `test_app.py` | End-to-end tests with Streamlit `AppTest` (Claude mocked) |
| `make_qr.py` | QR code for any URL |

## Differences from the web version

- State lives in the browser **session** (Streamlit has no localStorage): refreshing the page clears progress. Use the share link or the downloads to keep work.
- Python runs in a restricted server process instead of the student's browser (Pyodide).
- The API key is held in the session, not in localStorage.

## Security note

Student code executes on the server. The subprocess has time/CPU/memory/file limits and an empty environment, but it is **not a hardened sandbox**: code can still read container files and open network connections. Fine for classroom demos; for a long-lived public deployment prefer the web version or container-level isolation. Never put secrets in a deployment you do not trust students with, and remember that `ANTHROPIC_API_KEY` in Secrets is reachable only by the app owner, not by student code (the subprocess environment is empty).
