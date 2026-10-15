"""DebugWise AI · Streamlit edition.

Feature-for-feature port of https://github.com/Stephaneguisard/debugwise-ai :
hypothesis -> experiment -> three-level support ladder -> revise/test -> explain -> transfer -> reflect,
optional Claude (bring your own key), learning records, share links, teacher view and assignments.
Student code runs in a resource-limited subprocess (sandbox.py).
"""
import json
import pandas as pd
import streamlit as st

import core
from core import FLAG_LABEL, LEVEL_NAMES, STEP_NAMES, MODELS, words, now_ms, since, clock
from sandbox import dispatch

st.set_page_config(page_title="DebugWise AI", page_icon="🐞", layout="wide")
st.markdown("""<style>
.block-container{padding-top:2.2rem;max-width:1400px}
h1,h2,h3{letter-spacing:-.01em}
.dw-steps{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 14px}
.dw-steps span{border:1px solid #d2dbe1;border-radius:999px;padding:2px 12px;font-size:.85rem;color:#5B6C76}
.dw-steps span.done{background:#DFF1E6;border-color:#2C7A4B;color:#2C7A4B}
.dw-tag{font-size:.75rem;border-radius:6px;padding:1px 8px;background:#E3EAF7;color:#2550A0}
</style>""", unsafe_allow_html=True)

S = st.session_state
for k, v in {"custom": [], "progress": {}, "recs": [], "cur": core.PRESETS[0]["id"], "role": "Student", "lastexp": {},
             "msgs": {}, "own_open": False, "seen_files": set(), "share": {}, "reset_armed": None, "pending_ask": None,
             "api_key": "", "model": "claude-sonnet-5-5", "intro": True}.items():
    S.setdefault(k, v)


# ------------------------------------------------------------------ helpers
def all_tasks():
    return core.PRESETS + S.custom


def task_by_id(tid):
    return next((t for t in all_tasks() if t["id"] == tid), None)


def prog(t):
    if t["id"] not in S.progress:
        S.progress[t["id"]] = core.blank_progress(t)
    return S.progress[t["id"]]


def secret_key():
    try:
        return st.secrets.get("ANTHROPIC_API_KEY", "")
    except Exception:
        return ""


def ai_key():
    return (S.api_key or "").strip() or secret_key()


def ai_on():
    return bool(ai_key())


def base_url():
    try:
        return st.context.url.split("?")[0]
    except Exception:
        return "https://YOUR-APP.streamlit.app/"


def msg(tid, name, text=None):
    if text is None:
        return S.msgs.get((tid, name), "")
    S.msgs[(tid, name)] = text


def bound(label, tid, name, value, setter, height=100, placeholder=None):
    """A text area whose content lives in the progress dict."""
    k = f"{tid}:{name}"
    S.setdefault(k, value)
    v = st.text_area(label, key=k, height=height, placeholder=placeholder)
    setter(v)
    return v


@st.cache_data(show_spinner=False)
def failure_and_diag(code, tests_json, stdin_json):
    r = dispatch({"type": "tests", "code": code, "tests": json.loads(tests_json), "stdin": json.loads(stdin_json)})
    if "rows" not in r:
        return None, None, r
    f = next((x for x in r["rows"] if not x["pass"]), None) or (r["rows"][0] if r["rows"] else None)
    d = None
    if f:
        fail = {"call": f["call"], "err": f.get("err"), "got": f["got"], "expect": f["expect"]} if not f["pass"] else None
        d = dispatch({"type": "diagnose", "code": code, "failure": fail})
        if not d.get("category"):
            d = None
    return f, d, r


def term(call, r, expect=None) -> str:
    s = f">>> {call or 'run program'}\n"
    if r.get("timeout"):
        return s + f"Stopped after {r['limit']} seconds. The code may be stuck in an infinite loop."
    if r.get("internal"):
        return s + str(r.get("error"))
    if r.get("out"):
        s += r["out"].rstrip("\n") + "\n"
    if r.get("ok"):
        s += r["value"] if r.get("has_value") else ("" if r.get("out") else "(no output)")
    else:
        s += r["err"]["tb"]
    return s + (f"\nexpected: {expect}" if expect else "")


def fail_view(f) -> str:
    if not f:
        return "No failing test found."
    run = f["call"] == "(run the program)"
    s = f">>> {'run program' if run else f['call']}\n"
    if f.get("out"):
        s += f["out"].rstrip("\n") + "\n"
    s += f["err"]["tb"] if f.get("err") else str(f["got"])
    return s + ("" if run else f"\nexpected: {f['expect']}")


def result_summary(r) -> str:
    if r.get("timeout"):
        return f"stopped after {r['limit']} s (possible infinite loop)"
    if r.get("internal"):
        return str(r.get("error"))
    if r.get("ok"):
        return r["value"] if r.get("has_value") else (r["out"].strip().split("\n")[-1] if r.get("out") else "(no output)")
    return f"{r['err']['type']}: {r['err']['msg']}"


def build_task(f, origin):
    tests, bad = core.parse_tests(f.get("testsSrc"))
    if bad:
        return {"error": f"Could not read this test line: {bad[0]}. Use: call => expected value, for example  total([1, 2]) => 3"}
    stdin = (f.get("stdin") or "").split("\n") if f.get("stdin") else []
    r = dispatch({"type": "tests", "code": f["code"], "tests": tests, "stdin": stdin})
    if r.get("timeout"):
        return {"error": "The code ran for more than 7 seconds. Check for an infinite loop."}
    if r.get("internal") or "rows" not in r:
        return {"error": r.get("error") or "Python is not ready yet. Try again."}
    bad_exp = next((x for x in r["rows"] if x.get("bad_expect")), None)
    if bad_exp:
        return {"error": f'The expected value in "{bad_exp["call"]}" is not valid Python. Strings need quotes, e.g. "horse".'}
    if all(x["pass"] for x in r["rows"]):
        return {"error": "All tests already pass on this code, so there is no bug to debug. Add a test that shows the wrong behaviour."
                if tests else "The program runs without errors. Add a test line (call => expected) that shows the wrong behaviour."}
    extra = now_ms() if origin == "own" else ""
    return {"task": {"id": core.task_id("m-" if origin == "own" else "t-", f["title"], f["code"], tests, extra), "title": f["title"],
                     "origin": origin, "problem": f["problem"], "instructions": f.get("instructions", ""), "expected": f.get("expected", ""),
                     "criteria": f.get("criteria", []), "code": f["code"], "tests": tests, "stdin": f.get("stdin", "")}}


def switch_task(tid):
    S.cur = tid
    S.own_open = False


# ------------------------------------------------------------------ deep links
def handle_query():
    qp = st.query_params
    try:
        if "task" in qp:
            t = core.import_task(core.unpack(qp["task"]))
            if not task_by_id(t["id"]):
                S.custom.append(t)
            S.cur, S.role = t["id"], "Student"
            S.msgs[("_", "toast")] = f"Assignment loaded: {t['title']}"
        elif "record" in qp:
            rec = core.parse_record("#record=" + qp["record"])
            add_record(rec)
            S.role = "Teacher"
            S.msgs[("_", "toast")] = "Record opened"
        elif qp.get("role") == "teacher":
            S.role = "Teacher"
    except Exception:
        S.msgs[("_", "toast")] = "That link could not be read. Ask for a new one."
    qp.clear()


def add_record(rec):
    rec = dict(rec)
    rec["id"] = "r" + core.task_id("", rec.get("student"), rec.get("taskId"), rec.get("sharedAt"))
    S.recs = [r for r in S.recs if r["id"] != rec["id"]] + [rec]
    S.sel_rec = rec["id"]
    return rec["id"]


if not S.get("_qp_done"):
    S["_qp_done"] = True
    if st.query_params:
        handle_query()
if S.msgs.get(("_", "toast")):
    st.toast(S.msgs.pop(("_", "toast")))


# ------------------------------------------------------------------ sidebar / header
with st.sidebar:
    st.markdown("## 🐞 DebugWise **AI**")
    st.caption("diagnose first, fix second")
    S.role = st.radio("View as", ["Student", "Teacher"], index=["Student", "Teacher"].index(S.role), horizontal=True)
    st.markdown("---")
    st.markdown("**AI settings**")
    st.caption("**Built-in diagnosis (default).** A rule-based Python analyser reads your code, the error and your hypothesis and gives "
               "hints at three levels. It recognises common beginner bugs.")
    st.caption("**Claude (optional).** Add your own Anthropic API key for diagnoses and follow-up answers on any code. The key stays in "
               "this browser session only and is sent only to api.anthropic.com. Use a key with a spending limit.")
    S.api_key = st.text_input("Anthropic API key", value=S.api_key, type="password", placeholder="sk-ant-…")
    S.model = st.selectbox("Model", list(MODELS), index=list(MODELS).index(S.model), format_func=MODELS.get)
    st.markdown(f"**Status:** {'🟢 AI: Claude (' + ('your key' if S.api_key.strip() else 'app key') + ')' if ai_on() else '⚪ AI: built-in diagnosis'}")
    st.markdown("---")
    st.caption("Python runs in a restricted process on the server (7 s time limit). Nothing is stored: learning records only travel "
               "as links or files you create.")

st.markdown("# DebugWise AI")
if S.intro:
    c1, c2 = st.columns([8, 1])
    c1.info("**How DebugWise works.** Pick a Python task (or paste your own). Write a hypothesis, run a small experiment, and only then "
            "unlock the next hint. Nothing is saved unless you create a learning record link or file.")
    if c2.button("Got it"):
        S.intro = False
        st.rerun()


# ------------------------------------------------------------------ student view
def student_view():
    tasks = all_tasks()
    ids = [t["id"] for t in tasks]
    if S.cur not in ids:
        S.cur = ids[0]
    c1, c2, c3 = st.columns([4, 2, 2])
    def label(i):
        t = task_by_id(i)
        return t["title"] + ("" if t.get("preset") else "  (teacher assignment)" if t.get("origin") != "own" else "  (my code)")
    labels = []
    for i in ids:                                  # labels must be unique so we can map the choice back to an id
        lb = label(i)
        labels.append(lb if lb not in labels else f"{lb} #{ids.index(i) + 1}")
    S.cur = ids[labels.index(c1.selectbox("Task", labels, index=ids.index(S.cur)))]
    t = task_by_id(S.cur)
    tid = t["id"]
    p = prog(t)
    c2.write("")
    if c2.button("✏️ Debug my own code", width="stretch"):
        S.own_open = not S.own_open
    c3.write("")
    armed = S.reset_armed == tid
    if c3.button("Click again to erase this task's work" if armed else "Start over", width="stretch"):
        if armed:
            S.progress.pop(tid, None)
            for k in [k for k in S if isinstance(k, str) and k.startswith(tid + ":")]:
                del S[k]
            S.lastexp.pop(tid, None)
            S.reset_armed = None
            st.toast("Task reset")
            st.rerun()
        S.reset_armed = tid

    if S.own_open:
        with st.form("own"):
            st.markdown("#### Debug my own Python code")
            st.caption("Paste a program that does not behave the way you expect. Add at least one test line that shows the wrong behaviour, "
                       "or leave tests empty if the program crashes when you run it.")
            prob = st.text_area("What should the program do?", placeholder="e.g. total_price(items) adds up the prices in a shopping cart.")
            code = st.text_area("Your code", height=200, placeholder="def total_price(items):\n    ...")
            tests_src = st.text_area("Tests (optional): one per line, call => expected value", height=90, placeholder="total_price([2, 3.5]) => 5.5")
            stdin = st.text_area("Input for input() (optional): one line per answer", height=70)
            ok = st.form_submit_button("Start debugging", type="primary")
        if ok:
            if not prob.strip() or not code.strip():
                st.error("Fill in what the program should do and paste your code.")
            else:
                with st.spinner("Checking your code…"):
                    r = build_task({"title": (prob.strip().split("\n")[0] or "My code")[:40], "problem": prob.strip(),
                                    "code": code.rstrip(), "testsSrc": tests_src, "stdin": stdin}, "own")
                if r.get("error"):
                    st.error(r["error"])
                else:
                    S.custom.append(r["task"])
                    switch_task(r["task"]["id"])
                    st.rerun()

    stdin_lines = (t.get("stdin") or "").split("\n") if t.get("stdin") else []
    with st.spinner("Starting Python…"):
        failure, diag, raw = failure_and_diag(t["code"], json.dumps(t["tests"]), json.dumps(stdin_lines))
    if failure is None and raw and "rows" not in raw:
        st.error(term("", raw))

    done = core.step_done(p)
    st.markdown('<div class="dw-steps">' + "".join(
        f'<span class="{"done" if d else ""}"><b>{i + 1}</b> {n}</span>' for i, (n, d) in enumerate(zip(STEP_NAMES, done))) + "</div>",
        unsafe_allow_html=True)

    left, right = st.columns([5, 7], gap="large")

    # ----------------------------- evidence column
    with left:
        tag = "Sample task" if t.get("preset") else "My own code" if t.get("origin") == "own" else "Teacher assignment"
        st.markdown(f"### {t['title']}  <span class='dw-tag'>{tag}</span>", unsafe_allow_html=True)
        st.markdown(f"**Problem** · {t['problem']}")
        if t.get("instructions"):
            st.markdown(f"**Instructions** · {t['instructions']}")
        if t.get("expected"):
            st.markdown(f"**Expected behaviour** · {t['expected']}")
        if t.get("criteria"):
            st.markdown("**Assessed on**\n" + "\n".join(f"- {c}" for c in t["criteria"]))
        st.markdown("**Original code** <span class='dw-tag'>read-only · always visible</span>", unsafe_allow_html=True)
        st.code(t["code"], language="python", line_numbers=True)
        st.markdown("**Observed failure**")
        st.code(fail_view(failure), language="text")

    # ----------------------------- workflow column
    with right:
        # ---- Step 1
        st.markdown("### Step 1 · Write your hypothesis")
        st.caption("Before asking for help, say what you think is wrong and why. Use the pattern “I think the bug is … because …”. "
                   "You need a new hypothesis before each hint.")
        hk = f"{tid}:hyp"
        S.setdefault(hk, "")

        def save_hyp():
            txt = S[hk].strip()
            last = p["hypotheses"][-1] if p["hypotheses"] else None
            if words(txt) < 10:
                msg(tid, "hyp", f"Write at least 10 words ({words(txt)} so far). Say what is wrong and why you think so.")
            elif last and last["text"].strip().lower() == txt.lower():
                msg(tid, "hyp", "This is the same as your last hypothesis. Update it with what your experiment showed.")
            else:
                p["hypotheses"].append({"text": txt, "at": now_ms()})
                S[hk] = ""
                msg(tid, "hyp", "Saved.")

        st.text_area("Your hypothesis", key=hk, height=100,
                     placeholder="I think the bug is on line … because when the loop runs, the variable … has the value …")
        st.button("Save hypothesis", on_click=save_hyp, type="primary")
        if msg(tid, "hyp"):
            st.caption(msg(tid, "hyp"))
        for i, h in reversed(list(enumerate(p["hypotheses"], 1))):
            st.markdown(f"> **Hypothesis {i}** · {since(h['at'], p['startedAt'])} in  \n> {h['text']}")

        # ---- Step 2
        st.markdown("### Step 2 · Run a small experiment")
        st.caption("Call the function with an input you choose, predict the result, then note what the result tells you. "
                   "Add print(…) to your revision to trace values.")
        ck = f"{tid}:expcall"
        S.setdefault(ck, failure["call"] if failure and failure["call"] != "(run the program)" else "")
        e1, e2 = st.columns([1, 3])
        target = e1.selectbox("Run against", ["Original code", "My revision"], key=f"{tid}:exptarget")
        call = e2.text_input("Call (leave empty to just run the program)", key=ck)
        expect = st.text_input("I expect the result to be…", key=f"{tid}:expexpect", placeholder="e.g. 90, or an error")
        with st.expander("Input for input() (optional)"):
            exp_stdin = st.text_area("one line per answer", value=t.get("stdin", ""), key=f"{tid}:expstdin", height=60)
        if st.button("Run experiment"):
            code = t["code"] if target == "Original code" else p["revised"]
            with st.spinner("Running…"):
                r = dispatch({"type": "run", "code": code, "call": call.strip() or None, "stdin": exp_stdin.split("\n") if exp_stdin else []})
            S.lastexp[tid] = {"call": call.strip(), "target": "orig" if target == "Original code" else "rev", "expect": expect.strip(),
                              "result": result_summary(r), "view": term(call.strip(), r)}
            msg(tid, "exp", "")
        le = S.lastexp.get(tid)
        if le:
            st.code(le["view"], language="text")
            nk = f"{tid}:expnote"
            S.setdefault(nk, "")

            def log_exp():
                note = S[nk].strip()
                if words(note) < 5:
                    msg(tid, "exp", "Write at least 5 words about what the result means.")
                    return
                x = {k: le[k] for k in ("call", "target", "expect", "result")}
                p["experiments"].append({**x, "note": note, "at": now_ms()})
                S.lastexp.pop(tid, None)
                S[nk] = ""
                msg(tid, "exp", "")

            st.text_area("What does this result tell you?", key=nk, height=80, placeholder="The result was … which means …")
            st.button("Log experiment", on_click=log_exp, type="primary")
            if msg(tid, "exp"):
                st.caption(msg(tid, "exp"))
        for i, x in reversed(list(enumerate(p["experiments"], 1))):
            st.markdown(f"> **Experiment {i}** · {'original' if x['target'] == 'orig' else 'revision'} · {since(x['at'], p['startedAt'])} in  \n"
                        f"> `{x['call'] or 'run program'}` → `{x['result']}`  \n> {x['note']}")

        # ---- Step 3
        st.markdown("### Step 3 · Support ladder")
        st.caption("Help comes one level at a time. Each level unlocks after a new hypothesis and a new logged experiment. "
                   "DebugWise will not rewrite your program.")
        d = p["diagnosis"]
        if d and d["confidence"] < 0.6:
            st.warning(f"**DebugWise is not sure about this diagnosis** (confidence {round(d['confidence'] * 100)}%). Treat the hints as "
                       "suggestions and check them with your own experiments. It is flagged for your teacher to review.")
        g = core.gate(p)
        nxt = len(p["hints"]) + 1
        for L in (1, 2, 3):
            got = next((h for h in p["hints"] if h["level"] == L), None)
            if got:
                y = got["payload"]
                with st.container(border=True):
                    st.markdown(f"**L{L} · {LEVEL_NAMES[L - 1]}**  <span class='dw-tag'>{'Claude' if y.get('source') == 'claude' else 'built-in'}</span>",
                                unsafe_allow_html=True)
                    if L == 1:
                        st.markdown(f"**Likely category** · {y['category']}  \n**Possible misconception** · {y['misconception']}")
                    if y.get("support"):
                        st.write(y["support"])
                    if y.get("fix_lines"):
                        st.code(y["fix_lines"], language="text")
                    if y.get("hypothesis_feedback"):
                        (st.success if y["hypothesis_status"] == "on_track" else st.error if y["hypothesis_status"] == "off_track" else st.info)(
                            "**On your hypothesis:** " + y["hypothesis_feedback"])
                    if y.get("note"):
                        st.caption(y["note"])
            elif L == nxt:
                with st.container(border=True):
                    st.markdown(f"**L{L} · {LEVEL_NAMES[L - 1]}**")
                    since_txt = " since your last hint" if p["hints"] else ""
                    st.markdown(f"{'✅' if g['hyp'] else '⬜'} A new hypothesis{since_txt}  \n{'✅' if g['exp'] else '⬜'} A new logged experiment{since_txt}")
                    ready = g["hyp"] and g["exp"] and (diag is not None or ai_on())
                    if st.button(f"Unlock level {L}", disabled=not ready, key=f"{tid}:unlock{L}", type="primary"):
                        payload = None
                        with st.spinner("Thinking…"):
                            if ai_on():
                                try:
                                    payload = core.claude_hint(ai_key(), S.model, t, failure, L, p)
                                except Exception as e:
                                    why = ("Claude rejected the API key" if any(c in str(e) for c in ("401", "403"))
                                           else "Claude is rate-limited right now" if "429" in str(e) else "Claude could not be reached")
                                    if diag:
                                        payload = core.engine_hint(diag, L, g["hypText"])
                                        payload["note"] = why + ", so this hint comes from the built-in diagnosis."
                                    else:
                                        msg(tid, "hint", why + ". Check AI settings and try again.")
                            elif diag:
                                payload = core.engine_hint(diag, L, g["hypText"])
                        if payload:
                            core.register_hint(p, L, payload)
                            msg(tid, "hint", "")
                        st.rerun()
                    if not (g["hyp"] and g["exp"]):
                        st.caption("Complete both steps above first.")
                    if msg(tid, "hint"):
                        st.error(msg(tid, "hint"))
            else:
                st.markdown(f"🔒 **L{L}** · {LEVEL_NAMES[L - 1]} · locked")

        with st.container(border=True):
            st.markdown("**Ask a follow-up question**")
            st.caption("Claude answers with guiding questions and will not go beyond the hint level you have unlocked." if ai_on()
                       else "Follow-up questions need Claude. Add your own API key under AI settings (sidebar). The three-level ladder above works without it.")
            ak = f"{tid}:ask"
            S.setdefault(ak, "")
            if S.pending_ask and S.pending_ask[0] == tid:
                q = S.pending_ask[1]
                S.pending_ask = None
                try:
                    with st.spinner("Thinking…"):
                        a = core.claude_ask(ai_key(), S.model, t, failure, p, q)
                    p["asks"].append({"q": q, "a": a, "at": now_ms(), "level": len(p["hints"])})
                    msg(tid, "ask", "")
                except Exception as e:
                    S[ak] = q
                    msg(tid, "ask", "Claude rejected the API key. Check AI settings." if any(c in str(e) for c in ("401", "403"))
                        else "Claude could not be reached. Try again.")

            def ask_cb():
                q = S[ak].strip()
                if not p["hints"]:
                    msg(tid, "ask", "Unlock level 1 first.")
                elif words(q) < 4:
                    msg(tid, "ask", "Write your question in a full sentence.")
                elif len(p["asks"]) >= 6:
                    msg(tid, "ask", "You have used the 6 follow-up questions for this task. Try another experiment.")
                else:
                    S.pending_ask = (tid, q)
                    S[ak] = ""
                    msg(tid, "ask", "")

            st.text_area("Your question", key=ak, height=80, disabled=not ai_on(),
                         placeholder="e.g. Why does Python allow a list index of 3 when there are only three items?")
            st.button("Ask DebugWise", on_click=ask_cb, disabled=not ai_on())
            if msg(tid, "ask"):
                st.caption(msg(tid, "ask"))
            for a in p["asks"]:
                st.markdown(f"> **You:** {a['q']}  \n> {a['a']}")

        # ---- Step 4
        st.markdown("### Step 4 · Revise and test")
        st.caption("Edit your copy of the code. The original stays on the left so you can compare.")
        rk = f"{tid}:revised"
        S.setdefault(rk, p["revised"])
        p["revised"] = st.text_area("Revised code", key=rk, height=240, label_visibility="collapsed")
        b1, b2, b3 = st.columns([1, 1, 3])
        run_t = b1.button("Run tests", type="primary")
        if b2.button("Reset to original"):
            S[rk] = t["code"]
            p["revised"] = t["code"]
            st.rerun()
        if run_t:
            with st.spinner("Running…"):
                r = dispatch({"type": "tests", "code": p["revised"], "tests": t["tests"], "stdin": stdin_lines})
            if r.get("timeout"):
                b3.error(f"Stopped after {r['limit']} s: possible infinite loop")
            elif r.get("internal") or "rows" not in r:
                b3.error(r.get("error") or "Could not run the tests.")
            else:
                p["tests"] = [{k: x.get(k) for k in ("call", "expect", "got", "pass", "out")} for x in r["rows"]]
                p["testRuns"] += 1
                st.rerun()
        if not p["tests"]:
            b3.caption(f"{len(t['tests'])} tests ready" if t["tests"] else "The program should run without errors")
        else:
            npass = sum(1 for r in p["tests"] if r["pass"])
            (b3.success if npass == len(p["tests"]) else b3.error)(f"{npass} / {len(p['tests'])} passing")
            st.dataframe(pd.DataFrame([{"Call": r["call"], "Expected": r["expect"],
                                        "Got": r["got"] + (("\n" + r["out"].strip()) if r.get("out") else ""),
                                        "": "pass" if r["pass"] else "fail"} for r in p["tests"]]),
                         hide_index=True, width="stretch")

        # ---- Step 5
        st.markdown("### Step 5 · Explain the bug in your own words")
        st.caption("Why did the original program fail? Describe what was happening to the program's state, not only what you changed.")
        def set_expl(v):
            p["explanation"] = v
        bound("Explanation", tid, "expl", p["explanation"], set_expl, 120,
              "The original failed because … When the program ran, … had the value … so …")
        if st.button("Get feedback on my explanation"):
            if words(p["explanation"]) < 15:
                msg(tid, "expl", "Write at least 15 words first.")
            else:
                msg(tid, "expl", "")
                with st.spinner("Checking…"):
                    try:
                        p["explFeedback"] = (core.claude_explanation(ai_key(), S.model, t, failure, p["explanation"].strip()) if ai_on()
                                             else core.engine_explanation(diag, p["explanation"]))
                    except Exception:
                        msg(tid, "expl", "Claude could not be reached. Try again, or remove the key in AI settings.")
        if msg(tid, "expl"):
            st.caption(msg(tid, "expl"))
        fb = p.get("explFeedback")
        if fb:
            (st.success if fb["kind"] == "ok" else st.error if fb["kind"] == "bad" else st.info)(fb["text"])

        # ---- Step 6
        st.markdown("### Step 6 · Transfer: same principle, new code")
        st.caption("Predict what this new code does before you run it, then say which debugging principle applies.")
        passing = bool(p["tests"] and all(r["pass"] for r in p["tests"]))
        if not passing:
            st.info("Unlocks when all tests pass in Step 4.")
        else:
            tr, generic = t.get("transfer"), not t.get("transfer")
            if not generic:
                st.code(tr["code"], language="python", line_numbers=True)
            st.markdown("**" + ("Write a new input that would have exposed the bug in the original code. Predict what the original returns for it, then run it to check."
                                if generic else tr["question"]) + "**")

            def set_pred(v):
                p["transfer"]["prediction"] = v
            bound("Your prediction (before running)", tid, "pred", p["transfer"]["prediction"], set_pred, 80, "I predict … because …")
            gk = f"{tid}:trcall"
            S.setdefault(gk, p["transfer"].get("call") or (t["tests"][0]["call"] if t["tests"] else ""))
            if generic:
                st.text_input("Call to run", key=gk)
            if st.button("Run to check"):
                if words(p["transfer"]["prediction"]) < 4:
                    st.toast("Write your prediction before running.")
                else:
                    call = S[gk] if generic else tr["call"]
                    p["transfer"]["call"] = call
                    r = dispatch({"type": "run", "code": t["code"] if generic else tr["code"], "call": call, "stdin": stdin_lines})
                    p["transfer"]["result"] = term(call, r)
                    p["transfer"]["ranAt"] = now_ms()
                    st.rerun()
            if p["transfer"]["result"]:
                st.code(p["transfer"]["result"], language="text")
                mk = f"{tid}:trmatch"
                m = st.radio("Did your prediction match?", ["Yes", "No"], key=mk, horizontal=True, index=None)
                p["transfer"]["matched"] = {"Yes": "yes", "No": "no"}.get(m, "")

                def set_why(v):
                    p["transfer"]["why"] = v
                bound("Which debugging principle from this task applies here?", tid, "why", p["transfer"]["why"], set_why, 80)

        # ---- Step 7
        st.markdown("### Step 7 · Reflect")
        for key, q in (("a", "What was your first guess, and how did your experiments change it?"),
                       ("b", "Which hint level did you need, and could you have found the bug without it?"),
                       ("c", "What will you check first next time you see a similar bug?")):
            bound(q, tid, "ref" + key, p["ref"][key], lambda v, key=key: p["ref"].__setitem__(key, v), 80)

        # ---- Record
        st.markdown("### Learning record")
        st.caption("Your hypotheses, experiments, hints, revision and reflection in one place. Share it with your teacher with a link, or keep a copy.")
        tests = p["tests"]
        summary = [("Hypotheses", len(p["hypotheses"])), ("Experiments", len(p["experiments"])), ("Hint levels used", f"{len(p['hints'])} of 3"),
                   ("Tests passing", f"{sum(r['pass'] for r in tests)}/{len(tests)}" if tests else "not run"),
                   ("Diagnosis", f"{p['diagnosis']['category']} ({round(p['diagnosis']['confidence'] * 100)}% confidence)" if p["diagnosis"] else "No hints requested")]
        st.dataframe(pd.DataFrame([(a, str(b)) for a, b in summary], columns=["Item", "Value"]), hide_index=True, width="stretch")
        if p["flags"]:
            st.markdown("**Review flags** · " + " · ".join(f"`{FLAG_LABEL.get(f['type'], f['type'])}`" for f in p["flags"]))
        name = st.text_input("Your name", key="student_name", placeholder="Full name")
        r1, r2, r3 = st.columns(3)
        if r1.button("Create share link", type="primary"):
            if not name.strip():
                msg(tid, "share", "Add your name first.")
            else:
                if words(p["explanation"]) < 15:
                    core.add_flag(p, "thin_expl", "Shared with an explanation under 15 words.")
                rec = core.record_obj(p, t, name.strip())
                add_record(rec)
                S.share[tid] = base_url() + "?record=" + core.pack(rec)
                msg(tid, "share", "")
        r2.download_button("Download (.md)", core.record_markdown(p, t, name.strip()), file_name=f"DebugWise_{tid}_learning-record.md",
                           mime="text/markdown", width="stretch")
        r3.download_button("Download (.json)", json.dumps(core.record_obj(p, t, name.strip())), file_name=f"DebugWise_{tid}_learning-record.json",
                           mime="application/json", width="stretch")
        if msg(tid, "share"):
            st.caption(msg(tid, "share"))
        if S.share.get(tid):
            st.caption(f"Copy this link and send it to your teacher ({len(S.share[tid])} characters)."
                       + (" It is long: if your chat app cuts it off, use Download (.json) instead." if len(S.share[tid]) > 7000 else ""))
            st.code(S.share[tid], language="text")


# ------------------------------------------------------------------ teacher view
def all_recs():
    return list(reversed(S.recs)) + core.EXAMPLES


def rec_timeline(r):
    p = r["progress"]
    ev = [(h["at"], "Hypothesis", h["text"]) for h in p["hypotheses"]]
    ev += [(x["at"], "Experiment", f"`{x.get('call') or 'run program'}` → `{x['result']}`  \n{x.get('note', '')}") for x in p["experiments"]]
    ev += [(h["at"], f"Hint level {h['level']}", (h["payload"].get("support") or h["payload"].get("fix_lines") or "")
            + (f" ({round(h['payload']['confidence'] * 100)}% confidence)" if h["payload"].get("confidence") is not None else "")) for h in p["hints"]]
    ev += [(a["at"], "Question", f"**{a['q']}**  \n{a['a']}") for a in p.get("asks", [])]
    return sorted(ev, key=lambda e: e[0] or 0)


def rec_detail(r):
    p = r["progress"]
    st.markdown(f"### {r['student']} · {r['taskTitle']}" + ("  `EXAMPLE DATA`" if r.get("example") else ""))
    for f in p["flags"]:
        (st.error if f["type"] in ("low_conf", "off_track_fix") else st.warning)(f"**{FLAG_LABEL.get(f['type'], f['type'])}.** {f.get('msg', '')}")
    if p.get("diagnosis"):
        c = round(p["diagnosis"]["confidence"] * 100)
        st.markdown(f"**AI diagnosis** · {p['diagnosis']['category']} {'🔴' if c < 60 else '🟢'} {c}% confidence")
    st.markdown("#### Process")
    ev = rec_timeline(r)
    for at, kind, html in ev:
        st.markdown(f"`{since(at, p['startedAt']) if at else '–'}` **{kind}** · {html}")
    if not ev:
        st.caption("Nothing recorded.")
    st.markdown("#### Explanation")
    st.write(p.get("explanation") or "_Not written_")
    tr, rf = p.get("transfer") or {}, p.get("ref") or {}
    c1, c2 = st.columns(2)
    c1.markdown(f"**Transfer**  \nPrediction: {tr.get('prediction') or '–'}  \nMatched: {tr.get('matched') or '–'}  \nPrinciple: {tr.get('why') or '–'}")
    c2.markdown("**Reflection**  \n" + ("  \n".join(v for v in (rf.get("a"), rf.get("b"), rf.get("c")) if v) or "_Not written_"))
    if r.get("code"):
        st.markdown("#### Original code")
        st.code(r["code"], language="python", line_numbers=True)
    if p.get("revised"):
        st.markdown("#### Revised code")
        st.code(p["revised"], language="python", line_numbers=True)


def teacher_view():
    tab1, tab2 = st.tabs(["Learning records", "Assignments"])
    with tab1:
        recs = all_recs()
        flagged = [r for r in recs if r["progress"]["flags"]]
        avg = sum(len(r["progress"]["hints"]) for r in recs) / len(recs) if recs else 0
        low = sum(any(f["type"] == "low_conf" for f in r["progress"]["flags"]) for r in recs)
        m = st.columns(4)
        m[0].metric("Records", len(recs), f"{len(S.recs)} yours, {len(core.EXAMPLES)} examples", delta_color="off")
        m[1].metric("Need review", len(flagged), "at least one flag", delta_color="off")
        m[2].metric("Low-confidence AI", low, "check the diagnosis", delta_color="off")
        m[3].metric("Avg hint levels", f"{avg:.1f}", "of 3 per record", delta_color="off")

        with st.expander("Open a student record", expanded=not S.recs):
            st.caption("Students send you a share link or a .json file. Paste the link here, or choose the file. Records stay in this browser session.")
            c1, c2 = st.columns([4, 1])
            pasted = c1.text_input("Paste a record link or JSON", key="rec_paste", label_visibility="collapsed", placeholder="Paste a record link or JSON")
            if c2.button("Open", width="stretch"):
                try:
                    o = core.parse_record(pasted)
                    add_record(o)
                    S.msgs[("_", "toast")] = f"Opened {o['student']}'s record."
                except Exception:
                    st.error("That does not look like a DebugWise record link or file.")
                else:
                    st.rerun()
            files = st.file_uploader("Choose file(s)", type="json", accept_multiple_files=True, key="rec_files")
            n = 0
            for f in files or []:
                if (f.name, f.size) in S.seen_files:
                    continue
                S.seen_files.add((f.name, f.size))
                try:
                    add_record(core.parse_record(f.getvalue().decode()))
                    n += 1
                except Exception:
                    st.error(f"{f.name}: not a valid DebugWise record.")
            if n:
                st.rerun()

        only = st.checkbox("Flagged only")
        shown = flagged if only else recs
        L, R = st.columns([2, 5], gap="large")
        with L:
            if not shown:
                st.caption("No records match.")
            else:
                ids = [r["id"] for r in shown]
                if S.get("sel_rec") not in ids:
                    S.sel_rec = ids[0]
                def lab(i):
                    r = next(x for x in shown if x["id"] == i)
                    p = r["progress"]
                    ps = f"{sum(t['pass'] for t in p['tests'])}/{len(p['tests'])}" if p.get("tests") else "–"
                    return (f"{r['student']} — {r['taskTitle']} · hints {len(p['hints'])}/3 · tests {ps}"
                            + (" · ⚑ " + ", ".join(FLAG_LABEL.get(f['type'], f['type']) for f in p["flags"]) if p["flags"] else "")
                            + ("  [EXAMPLE]" if r.get("example") else f"  [{clock(r['sharedAt'])}]"))
                rl = [lab(i) for i in ids]
                rl = [x if rl.index(x) == n else f"{x} #{n + 1}" for n, x in enumerate(rl)]
                S.sel_rec = ids[rl.index(st.radio("Records", rl, index=ids.index(S.sel_rec), label_visibility="collapsed"))]
        with R:
            if shown:
                rec_detail(next(r for r in shown if r["id"] == S.sel_rec))

    with tab2:
        mine = [t for t in S.custom if t.get("origin") == "teacher"]
        L, R = st.columns([1, 1], gap="large")
        with L:
            if not mine:
                st.caption("Assignments you create appear here with a link to give your students.")
            for t in mine + core.PRESETS:
                with st.container(border=True):
                    st.markdown(f"**{t['title']}**  <span class='dw-tag'>{'sample' if t.get('preset') else 'yours'}</span>", unsafe_allow_html=True)
                    st.caption(t["problem"])
                    a, b, c = st.columns(3)
                    if a.button("Student link", key=f"lnk-{t['id']}"):
                        S.share["lnk:" + t["id"]] = base_url() + "?task=" + core.pack(core.task_share_obj(t))
                    if b.button("Open as student", key=f"opn-{t['id']}"):
                        S.role = "Student"
                        switch_task(t["id"])
                        st.rerun()
                    if not t.get("preset"):
                        armed = S.reset_armed == "del:" + t["id"]
                        if c.button("Click again to delete" if armed else "Delete", key=f"del-{t['id']}"):
                            if armed:
                                S.custom = [x for x in S.custom if x["id"] != t["id"]]
                                S.reset_armed = None
                                st.rerun()
                            S.reset_armed = "del:" + t["id"]
                    if S.share.get("lnk:" + t["id"]):
                        st.code(S.share["lnk:" + t["id"]], language="text")
        with R:
            with st.form("newtask", clear_on_submit=False):
                st.markdown("#### New assignment")
                st.caption("You get a link to give students. The task is stored inside the link, so no account or server is needed. "
                           "Code runs as Python in a restricted process.")
                title = st.text_input("Title", placeholder="e.g. Shopping cart total")
                problem = st.text_area("Problem", placeholder="What the program is supposed to do.")
                instr = st.text_area("Instructions", placeholder="What students should do and submit.")
                expected = st.text_input("Expected behaviour", placeholder="e.g. cart_total([2, 3.5]) returns 5.5")
                crit = st.text_area("Assessment criteria (one per line)", placeholder="Hypothesis names the faulty state\nExplanation describes why the original failed")
                code = st.text_area("Buggy Python code", height=220)
                tsrc = st.text_area("Tests (one per line: call => expected value, written in Python)", height=100, placeholder="cart_total([2, 3.5]) => 5.5")
                ok = st.form_submit_button("Create assignment", type="primary")
            if ok:
                if not title.strip() or not problem.strip() or not code.strip():
                    st.error("Title, problem and code are required.")
                else:
                    with st.spinner("Checking the code…"):
                        r = build_task({"title": title.strip(), "problem": problem.strip(), "instructions": instr.strip(), "expected": expected.strip(),
                                        "criteria": [c.strip() for c in crit.split("\n") if c.strip()], "code": code.rstrip(), "testsSrc": tsrc}, "teacher")
                    if r.get("error"):
                        st.error(r["error"])
                    elif task_by_id(r["task"]["id"]):
                        st.warning("An identical assignment already exists.")
                    else:
                        S.custom.append(r["task"])
                        st.success("Created. Use “Student link” to share it.")
                        st.rerun()


student_view() if S.role == "Student" else teacher_view()
st.markdown("---")
st.caption("DebugWise AI is a learning-design prototype. Python runs in a restricted server process (7 s limit). Nothing is stored; "
           "learning records only travel as links or files you create.")
