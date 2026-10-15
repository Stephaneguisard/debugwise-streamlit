import json

import core
from streamlit.testing.v1 import AppTest

HYP = "I think the bug is on line 3 because range(len(words) + 1) makes the index go one past the end of the list"
EXP_NOTE = "It raised IndexError so the index goes out of range"


def app():
    return AppTest.from_file("streamlit_app.py", default_timeout=90).run()


def by_label(at, kind, label):
    return next(w for w in getattr(at, kind) if w.label == label)


def btn(at, label):
    return next(b for b in at.button if b.label == label)


def do_hyp_and_exp(at, hyp=HYP):
    by_label(at, "text_area", "Your hypothesis").set_value(hyp)
    btn(at, "Save hypothesis").click().run()
    btn(at, "Run experiment").click().run()
    by_label(at, "text_area", "What does this result tell you?").set_value(EXP_NOTE)
    btn(at, "Log experiment").click().run()


def test_student_ladder_gating():
    at = app()
    assert not at.exception
    do_hyp_and_exp(at)
    assert not btn(at, "Unlock level 1").disabled
    btn(at, "Unlock level 1").click().run()
    assert any("Off-by-one" in m.value for m in at.markdown)
    assert btn(at, "Unlock level 2").disabled           # needs a new hypothesis + experiment
    p = list(at.session_state.progress.values())[0]
    assert len(p["hints"]) == 1 and p["hints"][0]["level"] == 1


def test_short_hypothesis_rejected():
    at = app()
    by_label(at, "text_area", "Your hypothesis").set_value("too short")
    btn(at, "Save hypothesis").click().run()
    assert any("at least 10 words" in c.value for c in at.caption)


def test_own_code_typed_and_debugged():
    at = app()
    btn(at, "✏️ Debug my own code").click().run()
    by_label(at, "text_area", "What should the program do?").set_value("total(items) adds the prices")
    by_label(at, "text_area", "Your code").set_value("def total(items):\n    s = 0\n    for i in items:\n        s = i\n    return s")
    by_label(at, "text_area", "Tests (optional): one per line, call => expected value").set_value("total([1, 2, 3]) => 6")
    [b for b in at.button if "Start debugging" in b.label][0].click().run()
    assert not at.exception
    assert at.session_state.cur.startswith("m-")
    do_hyp_and_exp(at, "I think the bug is on line 4 because the accumulator s is overwritten each time instead of added to")
    btn(at, "Unlock level 1").click().run()
    assert not at.exception
    assert len(list(at.session_state.progress[at.session_state.cur]["hints"])) == 1


def test_own_code_rejects_passing_code():
    at = app()
    btn(at, "✏️ Debug my own code").click().run()
    by_label(at, "text_area", "What should the program do?").set_value("add")
    by_label(at, "text_area", "Your code").set_value("def add(a, b):\n    return a + b")
    by_label(at, "text_area", "Tests (optional): one per line, call => expected value").set_value("add(1, 2) => 3")
    [b for b in at.button if "Start debugging" in b.label][0].click().run()
    assert any("no bug to debug" in e.value for e in at.error)


def test_claude_hint_and_followup(monkeypatch):
    calls = []

    def fake(key, model, prompt, max_tokens=900):
        calls.append(prompt)
        if "Reply with only JSON" in prompt and "LEVEL" in prompt:
            return json.dumps({"category": "Off-by-one", "misconception": "m", "confidence": 0.9, "hypothesis_status": "on_track",
                               "hypothesis_feedback": "good", "support": "What values does i take?", "fix_lines": ""})
        return "Try printing i inside the loop."
    monkeypatch.setattr(core, "claude_text", fake)
    at = app()
    at.session_state.api_key = "sk-test"
    at.run()
    do_hyp_and_exp(at)
    btn(at, "Unlock level 1").click().run()
    p = list(at.session_state.progress.values())[0]
    assert p["hints"][0]["payload"]["source"] == "claude"
    by_label(at, "text_area", "Your question").set_value("Why does the loop go one step too far here?")
    btn(at, "Ask DebugWise").click().run()
    p = list(at.session_state.progress.values())[0]
    assert p["asks"] and "printing" in p["asks"][0]["a"]
    assert any("Socratic" in c for c in calls)


def test_claude_failure_falls_back(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("429 slow down")
    monkeypatch.setattr(core, "claude_text", boom)
    at = app()
    at.session_state.api_key = "sk-test"
    at.run()
    do_hyp_and_exp(at)
    btn(at, "Unlock level 1").click().run()
    h = list(at.session_state.progress.values())[0]["hints"][0]["payload"]
    assert h["source"] == "engine" and "rate-limited" in h["note"]


def test_revise_and_tests_and_record():
    at = app()
    tid = at.session_state.cur
    at.session_state[f"{tid}:revised"] = next(t for t in core.PRESETS if t["id"] == tid)["code"].replace("len(words) + 1", "len(words)")
    at.run()
    btn(at, "Run tests").click().run()
    p = at.session_state.progress[tid]
    assert p["tests"] and all(r["pass"] for r in p["tests"]), p["tests"]
    by_label(at, "text_input", "Your name").set_value("Ann")
    btn(at, "Create share link").click().run()
    link = at.session_state.share[tid]
    assert "?record=" in link
    rec = core.parse_record(link)
    assert rec["student"] == "Ann" and rec["progress"]["tests"]


def test_teacher_open_record_link_and_assignment():
    at = app()
    at.session_state.role = "Teacher"
    at.run()
    assert not at.exception
    assert len(at.metric) == 4 and at.metric[0].value == "3"        # three example records
    t = core.PRESETS[0]
    rec = core.record_obj(core.blank_progress(t), t, "Bob")
    by_label(at, "text_input", "Paste a record link or JSON").set_value("https://x/?record=" + core.pack(rec))
    btn(at, "Open").click().run()
    assert at.metric[0].value == "4"
    # assignment
    form_fields = {w.label: w for w in list(at.text_input) + list(at.text_area)}
    form_fields["Title"].set_value("Cart total")
    form_fields["Problem"].set_value("sum prices")
    form_fields["Buggy Python code"].set_value("def cart_total(xs):\n    return sum(xs) - 1")
    form_fields["Tests (one per line: call => expected value, written in Python)"].set_value("cart_total([2, 3]) => 5")
    [b for b in at.button if b.label == "Create assignment"][0].click().run()
    assert any(t["title"] == "Cart total" and t["origin"] == "teacher" for t in at.session_state.custom)


def test_task_link_deeplink():
    t = {"title": "Linked", "problem": "p", "code": "def f():\n    return 2", "tests": [{"call": "f()", "expect": "1"}]}
    at = AppTest.from_file("streamlit_app.py", default_timeout=90)
    at.query_params["task"] = core.pack(t)
    at.run()
    assert not at.exception
    assert any(x["title"] == "Linked" for x in at.session_state.custom)
    assert at.session_state.cur.startswith("t-")
