"""DebugWise core logic (no Streamlit): tasks, gating, flags, Claude prompts, records, share links.

Ported from the web version (debugwise-ai/src/app.js) so both editions behave the same and
share links / .json records are interchangeable.
"""
import base64
import json
import re
import time
import zlib
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).parent
PRESETS = json.loads((HERE / "tasks.json").read_text())
EXAMPLES = json.loads((HERE / "examples.json").read_text())

FLAG_LABEL = {"low_conf": "Low-confidence diagnosis", "rapid": "Rapid hint requests", "early_fix": "Early jump to fix",
              "off_track_fix": "Fix reached with off-track hypothesis", "thin_expl": "Thin explanation"}
LEVEL_NAMES = ["Bug category + diagnostic question", "Narrowed hint", "Possible fix (changed lines only)"]
STEP_NAMES = ["Hypothesis", "Experiment", "Support", "Revise", "Explain", "Transfer", "Reflect"]
STATUS_COPY = {
    "on_track": "Your hypothesis is pointing in the right direction. Use your next experiment to confirm it.",
    "partly": "Part of your hypothesis is useful. Narrow it down: which exact value is wrong, and on which line does it go wrong?",
    "off_track": "Your hypothesis does not match the evidence yet. Compare what you expected with what your experiment actually returned.",
}
MODELS = {"claude-sonnet-5-5": "Claude Sonnet 5.5 (balanced)", "claude-haiku-5-5": "Claude Haiku 5.5 (fastest)",
          "claude-opus-5-5": "Claude Opus 5.5 (most capable)"}


def now_ms() -> int:
    return int(time.time() * 1000)


def words(s) -> int:
    return len((s or "").split())


def since(t, start) -> str:
    s = max(0, round((t - start) / 1000))
    return f"{s // 60}:{s % 60:02d}"


def clock(t) -> str:
    return datetime.fromtimestamp(t / 1000).strftime("%H:%M")


def numbered(code: str) -> str:
    return "\n".join(f"{i + 1:>2}  {l}" for i, l in enumerate(code.split("\n")))


# ---------------------------------------------------------------- progress / gating
def blank_progress(task) -> dict:
    return {"startedAt": now_ms(), "hypotheses": [], "experiments": [], "hints": [], "asks": [], "revised": task["code"],
            "tests": None, "testRuns": 0, "explanation": "", "explFeedback": None,
            "transfer": {"prediction": "", "result": None, "matched": "", "why": ""},
            "ref": {"a": "", "b": "", "c": ""}, "flags": [], "diagnosis": None}


def gate(p):
    last = p["hints"][-1]["at"] if p["hints"] else 0
    h = [x for x in p["hypotheses"] if x["at"] > last]
    e = [x for x in p["experiments"] if x["at"] > last]
    return {"hyp": bool(h), "exp": bool(e), "hypText": h[-1]["text"] if h else ""}


def add_flag(p, typ, msg):
    if not any(f["type"] == typ for f in p["flags"]):
        p["flags"].append({"type": typ, "msg": msg, "at": now_ms()})


def hyp_status(text, diag) -> str:
    t = (text or "").lower()
    hits = sum(1 for k in diag.get("keywords") or [] if str(k).lower() in t)
    if diag.get("line") and re.search(r"\b%s\b" % diag["line"], t):
        hits += 1
    return "on_track" if hits >= 3 else "partly" if hits >= 1 else "off_track"


def engine_hint(diag, level, hyp) -> dict:
    st = hyp_status(hyp, diag)
    return {"category": diag["category"], "misconception": diag["misconception"], "confidence": diag["confidence"],
            "hypothesis_status": st, "hypothesis_feedback": STATUS_COPY[st],
            "support": diag.get(f"l{level}") or "" if level < 3 else "", "fix_lines": diag.get("l3") or "" if level == 3 else "",
            "source": "engine"}


def register_hint(p, level, payload):
    """Append a hint and raise the review flags, exactly like requestHint() in the web version."""
    now = now_ms()
    prev = p["hints"][-1]["at"] if p["hints"] else None
    p["hints"].append({"level": level, "at": now, "payload": payload})
    if level == 1 or not p["diagnosis"]:
        p["diagnosis"] = {"category": payload["category"], "confidence": payload["confidence"]}
    else:
        p["diagnosis"]["confidence"] = min(p["diagnosis"]["confidence"], payload["confidence"])
    if payload["confidence"] < 0.6:
        add_flag(p, "low_conf", f"Diagnosis confidence {round(payload['confidence'] * 100)}% ({payload['category']}).")
    if prev and now - prev < 60_000:
        add_flag(p, "rapid", f"Level {level} requested {round((now - prev) / 1000)} s after level {level - 1}.")
    if level == 3 and now - p["startedAt"] < 4 * 60_000:
        add_flag(p, "early_fix", f"Reached the fix level {since(now, p['startedAt'])} after starting.")
    if level == 3 and payload["hypothesis_status"] == "off_track":
        add_flag(p, "off_track_fix", "Latest hypothesis was off track when the fix was shown; the misconception may persist.")


def step_done(p) -> list:
    tr = p["transfer"]
    return [len(p["hypotheses"]) > 0, len(p["experiments"]) > 0, len(p["hints"]) > 0,
            bool(p["tests"] and all(r["pass"] for r in p["tests"])), words(p["explanation"]) >= 15,
            bool(tr.get("result") and tr.get("matched") and words(tr.get("why")) >= 5),
            all(words(p["ref"][k]) >= 5 for k in "abc")]


# ---------------------------------------------------------------- Claude (optional, bring your own key)
def claude_text(key, model, prompt, max_tokens=900) -> str:
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                      headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"},
                      json={"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})
    if not r.ok:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = ""
        raise RuntimeError(f"{r.status_code} {msg}".strip())
    return "".join(c.get("text", "") for c in r.json().get("content", []))


def parse_json(t):
    m = re.search(r"\{[\s\S]*\}", t)
    return json.loads(m.group(0) if m else t)


def fail_text(f) -> str:
    if not f:
        return "(not run yet)"
    s = f"> {f['call']}\n"
    if f.get("out"):
        s += f["out"].strip() + "\n"
    s += f["err"]["tb"] if f.get("err") else f"result: {f['got']}"
    if f["call"] != "(run the program)":
        s += f"\nexpected: {f['expect']}"
    return s


def claude_hint(key, model, task, failure, level, p) -> dict:
    hyps = "\n".join(f"{i + 1}. {h['text']}" for i, h in enumerate(p["hypotheses"])) or "(none)"
    exps = "\n".join(
        f"{i + 1}. ran {x['call'] or 'the program'} on {'original' if x['target'] == 'orig' else 'revised'} code; "
        f"expected {x['expect'] or '(not stated)'}; got {x['result']}; student note: {x['note']}"
        for i, x in enumerate(p["experiments"])) or "(none)"
    prev = "\n".join(f"Level {h['level']}: {h['payload'].get('support') or h['payload'].get('fix_lines')}" for h in p["hints"]) or "(none)"
    prompt = f"""You are DebugWise AI, a debugging tutor for undergraduate novice programmers learning Python. You support the student's own diagnosis with tiered scaffolding. Never return a full corrected program.

ASSIGNMENT: {task['title']}
Problem: {task['problem']}
Expected behaviour: {task.get('expected') or '(not given)'}

ORIGINAL CODE (Python, line-numbered):
{numbered(task['code'])}

OBSERVED FAILURE:
{fail_text(failure)}

STUDENT HYPOTHESES (oldest first):
{hyps}

STUDENT EXPERIMENTS:
{exps}

HINTS ALREADY GIVEN:
{prev}

The student is requesting support LEVEL {level} of 3.
- Level 1: name the likely bug category and the misconception behind it, and ask ONE diagnostic question that makes the student inspect program state. No line number, no fix.
- Level 2: a narrowed hint that points to the relevant line range and says what to trace or print. No corrected code.
- Level 3: a minimal possible fix: only the changed line(s), with one sentence on why. Never the whole program.
Also judge the student's LATEST hypothesis against the real bug and give 1-2 sentences of feedback that reveal no more than the current level allows.
Set confidence honestly from 0 to 1: how sure you are that your diagnosis is the real cause. Use below 0.6 if the code or failure is ambiguous.

Reply with only JSON:
{{"category": string, "misconception": string, "confidence": number, "hypothesis_status": "on_track"|"partly"|"off_track", "hypothesis_feedback": string, "support": string, "fix_lines": string}}
For levels 1 and 2, fix_lines must be "". For level 3, support may be ""."""
    r = parse_json(claude_text(key, model, prompt, 900))
    try:
        conf = max(0.0, min(1.0, float(r.get("confidence"))))
    except (TypeError, ValueError):
        conf = 0.5
    st = r.get("hypothesis_status")
    return {"category": str(r.get("category") or "Unclassified"), "misconception": str(r.get("misconception") or ""), "confidence": conf,
            "hypothesis_status": st if st in ("on_track", "partly", "off_track") else "partly",
            "hypothesis_feedback": str(r.get("hypothesis_feedback") or ""), "support": str(r.get("support") or ""),
            "fix_lines": str(r.get("fix_lines") or "") if level == 3 else "", "source": "claude"}


def claude_ask(key, model, task, failure, p, q) -> str:
    hyps = "\n".join(f"{i + 1}. {h['text']}" for i, h in enumerate(p["hypotheses"]))
    lvl = len(p["hints"])
    prompt = f"""You are DebugWise AI, a Socratic Python debugging tutor for novices. The student has unlocked support level {lvl} of 3 (1 = category and question, 2 = narrowed hint, 3 = changed lines only). Answer their follow-up question in under 120 words. Do not reveal more than level {lvl} allows. Never write a corrected program. Prefer a guiding question or a small experiment they can run.

ORIGINAL CODE:
{numbered(task['code'])}

OBSERVED FAILURE:
{fail_text(failure)}

STUDENT HYPOTHESES:
{hyps or '(none)'}

QUESTION: {q}"""
    return claude_text(key, model, prompt, 500).strip()


def claude_explanation(key, model, task, failure, text) -> dict:
    prompt = f"""You are a Python debugging tutor. A novice fixed a bug and explained it. Judge only the explanation of WHY the original code failed. Do not give corrected code.

Original code:
{numbered(task['code'])}

Observed failure:
{fail_text(failure)}

Student explanation:
{text}

Reply with only JSON: {{"rating": "strong"|"partial"|"weak", "feedback": string (max 60 words: what is accurate, and what is missing about program state or Python's rules)}}"""
    r = parse_json(claude_text(key, model, prompt, 400))
    kind = "ok" if r.get("rating") == "strong" else "bad" if r.get("rating") == "weak" else "info"
    return {"source": "claude", "kind": kind, "text": str(r.get("feedback") or "")}


def engine_explanation(diag, text) -> dict:
    st = hyp_status(text, diag) if diag else "partly"
    msg = {"on_track": "Your explanation names the key idea. Check that it also says what value the program actually had when it failed, and which Python rule produced that.",
           "partly": "You touch on the cause. Add what the program state was (which variable, which value) and which Python rule explains it.",
           "off_track": "The explanation does not yet name the cause. Describe what the program did step by step, compared with what you expected."}[st]
    return {"source": "engine", "kind": "ok" if st == "on_track" else "bad" if st == "off_track" else "info", "text": msg}


# ---------------------------------------------------------------- tests syntax / task building
def parse_tests(src):
    out, bad = [], []
    for line in (l.strip() for l in (src or "").split("\n")):
        if not line:
            continue
        i = line.rfind("=>")
        if i < 0 or not line[:i].strip() or not line[i + 2:].strip():
            bad.append(line)
        else:
            out.append({"call": line[:i].strip(), "expect": line[i + 2:].strip()})
    return out, bad


def task_id(prefix, *parts) -> str:
    h = 5381
    for ch in json.dumps(parts, ensure_ascii=False):
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    return prefix + format(h, "x")


# ---------------------------------------------------------------- sharing (same format as the web version)
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def pack(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return "z" + _b64u(c.compress(raw) + c.flush())


def unpack(s: str):
    mode, body = s[0], _unb64u(s[1:])
    if mode == "z":
        return json.loads(zlib.decompress(body, -15).decode())
    if mode == "r":
        return json.loads(body.decode())
    raise ValueError("bad link")


def record_obj(p, t, name) -> dict:
    return {"v": 1, "student": name, "taskId": t["id"], "taskTitle": t["title"], "sharedAt": now_ms(), "code": t["code"],
            "task": None if t.get("preset") else {"title": t["title"], "problem": t["problem"]}, "progress": p}


def task_share_obj(t) -> dict:
    return {"v": 1, "title": t["title"], "problem": t["problem"], "instructions": t.get("instructions", ""),
            "expected": t.get("expected", ""), "criteria": t.get("criteria", []), "code": t["code"], "tests": t["tests"],
            "stdin": t.get("stdin", "")}


def import_task(o) -> dict:
    if not o or not o.get("code") or not o.get("title") or not isinstance(o.get("tests"), list):
        raise ValueError("bad task")
    tid = task_id("t-", o["title"], o["code"], o["tests"])
    return {"id": tid, "title": str(o["title"]), "origin": "link", "problem": str(o.get("problem") or ""),
            "instructions": str(o.get("instructions") or ""), "expected": str(o.get("expected") or ""),
            "criteria": [str(c) for c in o.get("criteria") or []], "code": str(o["code"]),
            "tests": [{"call": str(x["call"]), "expect": str(x["expect"])} for x in o["tests"]], "stdin": str(o.get("stdin") or "")}


def parse_record(s: str) -> dict:
    s = s.strip()
    m = re.search(r"[#?]record=([A-Za-z0-9_\-]+)", s)
    obj = unpack(m.group(1)) if m else json.loads(s) if s[:1] == "{" else unpack(s)
    if not obj or not obj.get("progress") or obj["progress"].get("hypotheses") is None or not obj.get("taskTitle"):
        raise ValueError("bad record")
    return obj


def record_markdown(p, t, name) -> str:
    L = [f"# DebugWise learning record: {t['title']}", "", f"Student: {name or '(not given)'}  ",
         f"Started: {datetime.fromtimestamp(p['startedAt'] / 1000).strftime('%Y-%m-%d %H:%M')}", "",
         "## Original code", "```python", t["code"], "```", "", "## Hypotheses"]
    L += [f"{i}. ({since(h['at'], p['startedAt'])}) {h['text']}" for i, h in enumerate(p["hypotheses"], 1)] + ["", "## Experiments"]
    L += [f"{i}. ({since(x['at'], p['startedAt'])}) `{x['call'] or 'run program'}` on {'original' if x['target'] == 'orig' else 'revision'} "
          f"→ `{x['result']}`; expected {x['expect'] or '-'}. Note: {x['note']}" for i, x in enumerate(p["experiments"], 1)] + ["", "## Hints used"]
    L += [f"- Level {h['level']} ({since(h['at'], p['startedAt'])}, {h['payload'].get('source')}): "
          f"{(h['payload'].get('support') or h['payload'].get('fix_lines') or '').replace(chr(10), ' ')}" for h in p["hints"]] + [""]
    if p.get("asks"):
        L += ["## Follow-up questions"]
        for a in p["asks"]:
            L += [f"- Q: {a['q']}", f"  A: {a['a'].replace(chr(10), ' ')}"]
        L += [""]
    tests = p["tests"]
    L += ["## Revised code", "```python", p["revised"], "```",
          f"Tests: {sum(r['pass'] for r in tests)}/{len(tests)} passing" if tests else "Tests: not run", "",
          "## Explanation", p["explanation"] or "-", "", "## Transfer", f"Prediction: {p['transfer'].get('prediction') or '-'}",
          f"Matched: {p['transfer'].get('matched') or '-'}", f"Principle: {p['transfer'].get('why') or '-'}", "",
          "## Reflection", f"- First guess and change: {p['ref']['a'] or '-'}", f"- Hint level needed: {p['ref']['b'] or '-'}",
          f"- Next time: {p['ref']['c'] or '-'}", ""]
    if p["flags"]:
        L += ["## Flags for instructor review"] + [f"- {FLAG_LABEL.get(f['type'], f['type'])}: {f['msg']}" for f in p["flags"]]
    return "\n".join(L)
