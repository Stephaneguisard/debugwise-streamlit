"""DebugWise · Streamlit edition.

Students diagnose Python bugs with hypotheses and experiments; help is released one level
at a time. Teachers can load learning records (also the .json files exported by the web
version) into a dashboard. Student code runs in a resource-limited subprocess (sandbox.py).
"""
import io
import json
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from sandbox import dispatch

st.set_page_config(page_title="DebugWise · 先诊断,再修复", page_icon="🐞", layout="wide")

TASKS = json.loads((Path(__file__).parent / "tasks.json").read_text())
FLAG_LABEL = {"low_conf": "低置信度诊断", "rapid": "连续快速求助", "early_fix": "过早要答案",
              "off_track_fix": "假设偏离却看到答案", "thin_expl": "解释过短"}
LEVEL_NAMES = ["缺陷类别 + 诊断问题", "缩小范围的提示", "可能的修改(仅改动的行)"]
STATUS_COPY = {
    "on_track": "你的假设方向正确,用下一次实验去确认它。",
    "partly": "假设有一部分有用。再收窄:具体是哪个值错了?在哪一行出错?",
    "off_track": "假设和证据还对不上。对比一下你的预期和实验的真实结果。",
}


def now_ms():
    return int(time.time() * 1000)


# ------------------------------------------------------------------ state
def blank_progress(task):
    return {"startedAt": now_ms(), "hypotheses": [], "experiments": [], "hints": [], "asks": [],
            "revised": task["code"], "tests": None, "testRuns": 0, "explanation": "", "explFeedback": None,
            "transfer": {"prediction": "", "result": None, "matched": "", "why": ""},
            "ref": {"a": "", "b": "", "c": ""}, "flags": [], "diagnosis": None}


S = st.session_state
S.setdefault("custom", [])
S.setdefault("progress", {})
S.setdefault("last_exp", None)


def all_tasks():
    return TASKS + S.custom


def prog(task):
    if task["id"] not in S.progress:
        S.progress[task["id"]] = blank_progress(task)
    return S.progress[task["id"]]


# ------------------------------------------------------------------ engine helpers
def run_tests(code, tests, stdin=None):
    r = dispatch({"type": "tests", "code": code, "tests": tests, "stdin": stdin or []})
    return r


@st.cache_data(show_spinner=False)
def failure_and_diagnosis(code, tests_json):
    tests = json.loads(tests_json)
    r = run_tests(code, tests)
    if "rows" not in r:
        return None, None, r
    fail = next((x for x in r["rows"] if not x["pass"]), None)
    diag = dispatch({"type": "diagnose", "code": code, "failure": fail}) if fail else None
    return fail, diag, r


def hyp_status(text, diag):
    t = (text or "").lower()
    hits = sum(1 for k in diag.get("keywords", []) if str(k).lower() in t)
    if diag.get("line") and f"{diag['line']}" in t:
        hits += 1
    return "on_track" if hits >= 3 else "partly" if hits >= 1 else "off_track"


def add_flag(p, typ, msg):
    if not any(f["type"] == typ for f in p["flags"]):
        p["flags"].append({"type": typ, "msg": msg, "at": now_ms()})


def gate(p):
    last = p["hints"][-1]["at"] if p["hints"] else 0
    h = [x for x in p["hypotheses"] if x["at"] > last]
    e = [x for x in p["experiments"] if x["at"] > last]
    return bool(h), bool(e), (h[-1]["text"] if h else "")


def term_text(call, r):
    if r.get("timeout"):
        return f"⏱ 运行超过 {r['limit']} 秒被终止,代码可能陷入死循环。"
    if r.get("internal"):
        return r.get("error", "内部错误")
    s = f">>> {call or '运行程序'}\n"
    if r.get("out"):
        s += r["out"].rstrip("\n") + "\n"
    if r.get("ok"):
        s += r["value"] if r.get("has_value") else ("" if r.get("out") else "(无输出)")
    else:
        s += r["err"]["tb"]
    return s


def fail_text(f):
    if not f:
        return "没有失败的测试。"
    run = f["call"] == "(run the program)"
    s = f">>> {'运行程序' if run else f['call']}\n"
    if f.get("out"):
        s += f["out"].rstrip("\n") + "\n"
    s += f["err"]["tb"] if f.get("err") else str(f["got"])
    if not run:
        s += f"\n期望: {f['expect']}"
    return s


def record_obj(p, task, name):
    return {"v": 1, "student": name, "taskId": task["id"], "taskTitle": task["title"], "sharedAt": now_ms(),
            "code": task["code"], "task": None if task.get("preset") else {"title": task["title"], "problem": task["problem"]},
            "progress": p}


def record_md(p, task, name):
    L = [f"# DebugWise 学习记录 · {task['title']}", f"学生:{name or '(未填写)'}", "", "## 假设"]
    L += [f"{i}. {h['text']}" for i, h in enumerate(p["hypotheses"], 1)] or ["(无)"]
    L += ["", "## 实验"]
    L += [f"{i}. `{x['call'] or '运行程序'}` → `{x['result']}`;{x['note']}" for i, x in enumerate(p["experiments"], 1)] or ["(无)"]
    L += ["", "## 提示"]
    L += [f"- L{h['level']}: {h['payload'].get('support') or h['payload'].get('fix_lines')}" for h in p["hints"]] or ["(未使用)"]
    L += ["", "## 修订后的代码", "```python", p["revised"], "```", "", "## 解释", p["explanation"] or "(未填写)", "",
          "## 反思", *[v for v in p["ref"].values() if v]]
    if p["flags"]:
        L += ["", "## 标记", *[f"- {FLAG_LABEL.get(f['type'], f['type'])}:{f['msg']}" for f in p["flags"]]]
    return "\n".join(L)


# ------------------------------------------------------------------ student page
def student_page():
    tasks = all_tasks()
    c1, c2 = st.columns([3, 1])
    with c1:
        idx = st.selectbox("选择任务", range(len(tasks)), format_func=lambda i: tasks[i]["title"] + ("" if tasks[i].get("preset") else "(我的代码)"))
    task = tasks[idx]
    p = prog(task)
    with c2:
        st.write("")
        if st.button("重新开始这个任务", use_container_width=True):
            S.progress[task["id"]] = blank_progress(task)
            S.last_exp = None
            st.rerun()

    with st.expander("➕ 调试我自己的代码"):
        with st.form("own"):
            prob = st.text_area("这个程序应该做什么?")
            code = st.text_area("你的代码", height=160)
            tests_src = st.text_area("测试(每行一条:`调用 => 期望值`,可留空)", height=70, placeholder="total_price([2, 3.5]) => 5.5")
            if st.form_submit_button("开始调试"):
                tests, bad = [], []
                for line in filter(None, (l.strip() for l in tests_src.splitlines())):
                    a, sep, b = line.rpartition("=>")
                    (tests.append({"call": a.strip(), "expect": b.strip()}) if sep and a.strip() and b.strip() else bad.append(line))
                if not prob.strip() or not code.strip():
                    st.error("请填写说明和代码。")
                elif bad:
                    st.error(f"无法解析这条测试:{bad[0]}")
                else:
                    r = run_tests(code, tests)
                    if "rows" not in r:
                        st.error(r.get("error") or "运行超时,检查是否有死循环。")
                    elif all(x["pass"] for x in r["rows"]):
                        st.error("所有测试都通过了,没有 bug 可调试。请加一条能暴露错误的测试。")
                    else:
                        S.custom.append({"id": f"m-{len(S.custom)}-{abs(hash(code)) % 10**6}", "title": prob.strip().splitlines()[0][:30],
                                         "problem": prob.strip(), "instructions": "", "expected": "", "criteria": [], "code": code,
                                         "tests": tests, "transfer": None})
                        st.rerun()

    fail, diag, raw = failure_and_diagnosis(task["code"], json.dumps(task["tests"]))
    if diag is None and fail is None and "rows" not in raw:
        st.error("Python 沙箱没能运行:" + str(raw.get("error", raw)))
        return

    left, right = st.columns([2, 3], gap="large")
    with left:
        st.subheader(task["title"])
        st.markdown(f"**问题** {task['problem']}")
        if task.get("instructions"):
            st.markdown(f"**要求** {task['instructions']}")
        if task.get("expected"):
            st.markdown(f"**期望行为** {task['expected']}")
        if task.get("criteria"):
            st.markdown("**评价维度**\n" + "\n".join(f"- {c}" for c in task["criteria"]))
        st.caption("原始代码(只读,始终可见)")
        st.code(task["code"], language="python", line_numbers=True)
        st.caption("观察到的失败现象")
        st.code(fail_text(fail), language="text")

    with right:
        # ---- step 1
        st.markdown("### 第 1 步 · 写下你的假设")
        st.caption("格式:“我认为 bug 在……,因为……”。每次要提示之前,都需要一条新假设。")
        with st.form("hyp", clear_on_submit=True):
            h = st.text_area("你的假设", height=90, label_visibility="collapsed")
            if st.form_submit_button("保存假设"):
                if len(h.split()) + len(h) // 6 < 4:
                    st.warning("请写得具体一点(至少一句完整的话)。")
                else:
                    p["hypotheses"].append({"text": h.strip(), "at": now_ms()})
                    st.rerun()
        for i, x in enumerate(p["hypotheses"], 1):
            st.markdown(f"**H{i}** {x['text']}")

        # ---- step 2
        st.markdown("### 第 2 步 · 做一个小实验")
        e1, e2 = st.columns([1, 3])
        target = e1.selectbox("运行对象", ["原始代码", "我的修订"])
        call = e2.text_input("调用(留空则只运行程序)", placeholder='longest_word(["a", "bb"])')
        expect = st.text_input("我预期的结果是……")
        stdin = st.text_area("input() 的输入(可选,每行一个)", height=50)
        if st.button("运行实验"):
            code = task["code"] if target == "原始代码" else p["revised"]
            r = dispatch({"type": "run", "code": code, "call": call or None, "stdin": stdin.splitlines()})
            S.last_exp = {"call": call, "target": "orig" if target == "原始代码" else "rev", "expect": expect, "r": r}
        le = S.last_exp
        if le:
            st.code(term_text(le["call"], le["r"]) + (f"\n预期: {le['expect']}" if le["expect"] else ""), language="text")
            note = st.text_area("这个结果说明了什么?", height=70, key="expnote")
            if st.button("记录实验"):
                if len(note.split()) + len(note) // 6 < 3:
                    st.warning("请写下这个结果对你的诊断意味着什么。")
                else:
                    r = le["r"]
                    res = (r.get("value") if r.get("ok") else (r["err"]["type"] if r.get("err") else "timeout")) or "(无返回值)"
                    p["experiments"].append({"call": le["call"], "target": le["target"], "expect": le["expect"], "result": str(res),
                                             "note": note.strip(), "at": now_ms()})
                    S.last_exp = None
                    st.rerun()
        for i, x in enumerate(p["experiments"], 1):
            st.markdown(f"**E{i}** `{x['call'] or '运行程序'}` → `{x['result']}` · {x['note']}")

        # ---- step 3
        st.markdown("### 第 3 步 · 支持阶梯")
        st.caption("每一级都需要:自上一条提示以来的新假设 + 新实验。DebugWise 不会替你重写程序。")
        if p["diagnosis"] and p["diagnosis"]["confidence"] < 0.6:
            st.warning(f"DebugWise 对这个诊断不太确定(置信度 {round(p['diagnosis']['confidence'] * 100)}%)。把提示当建议,用你自己的实验验证。已标记给老师复核。")
        gh, ge, last_hyp = gate(p)
        nxt = len(p["hints"]) + 1
        for L in (1, 2, 3):
            got = next((x for x in p["hints"] if x["level"] == L), None)
            if got:
                y = got["payload"]
                with st.container(border=True):
                    st.markdown(f"**L{L} · {LEVEL_NAMES[L - 1]}**")
                    if L == 1:
                        st.markdown(f"可能的类别:{y['category']}  \n可能的误解:{y['misconception']}")
                    if y.get("support"):
                        st.write(y["support"])
                    if y.get("fix_lines"):
                        st.code(y["fix_lines"], language="text")
                    if y.get("hypothesis_feedback"):
                        st.info("关于你的假设:" + y["hypothesis_feedback"])
            elif L == nxt:
                with st.container(border=True):
                    st.markdown(f"**L{L} · {LEVEL_NAMES[L - 1]}**")
                    st.markdown(f"{'✅' if gh else '⬜'} 一条新假设  \n{'✅' if ge else '⬜'} 一次新记录的实验")
                    if st.button(f"解锁第 {L} 级", disabled=not (gh and ge and diag)):
                        st_ = hyp_status(last_hyp, diag)
                        payload = {"category": diag["category"], "misconception": diag["misconception"], "confidence": diag["confidence"],
                                   "hypothesis_status": st_, "hypothesis_feedback": STATUS_COPY[st_],
                                   "support": diag[f"l{L}"] if L < 3 else "", "fix_lines": diag["l3"] if L == 3 else ""}
                        t = now_ms()
                        prev = p["hints"][-1]["at"] if p["hints"] else None
                        p["hints"].append({"level": L, "at": t, "payload": payload})
                        if L == 1 or not p["diagnosis"]:
                            p["diagnosis"] = {"category": diag["category"], "confidence": diag["confidence"]}
                        else:
                            p["diagnosis"]["confidence"] = min(p["diagnosis"]["confidence"], diag["confidence"])
                        if diag["confidence"] < 0.6:
                            add_flag(p, "low_conf", f"诊断置信度 {round(diag['confidence'] * 100)}%({diag['category']})。")
                        if prev and t - prev < 60_000:
                            add_flag(p, "rapid", f"第 {L} 级在第 {L - 1} 级之后 {round((t - prev) / 1000)} 秒就被请求。")
                        if L == 3 and t - p["startedAt"] < 4 * 60_000:
                            add_flag(p, "early_fix", "开始后不到 4 分钟就看了修改提示。")
                        if L == 3 and st_ == "off_track":
                            add_flag(p, "off_track_fix", "展示修改时,最新假设仍然偏离,误解可能仍在。")
                        st.rerun()
            else:
                st.markdown(f"🔒 L{L} · {LEVEL_NAMES[L - 1]}")

        # ---- step 4
        st.markdown("### 第 4 步 · 修改并测试")
        p["revised"] = st.text_area("你的修订版(原始代码在左边,方便对照)", value=p["revised"], height=200, key=f"rev-{task['id']}")
        if st.button("运行测试"):
            r = run_tests(p["revised"], task["tests"])
            if "rows" in r:
                p["tests"] = [{k: x[k] for k in ("call", "expect", "got", "pass")} for x in r["rows"]]
                p["testRuns"] += 1
            else:
                st.error("运行超时或出错:" + str(r.get("error", "可能有死循环")))
        if p["tests"]:
            ok = sum(x["pass"] for x in p["tests"])
            (st.success if ok == len(p["tests"]) else st.error)(f"{ok} / {len(p['tests'])} 通过")
            st.dataframe(pd.DataFrame(p["tests"]).rename(columns={"call": "调用", "expect": "期望", "got": "实际", "pass": "通过"}),
                         hide_index=True, use_container_width=True)

        # ---- step 5
        st.markdown("### 第 5 步 · 用自己的话解释这个 bug")
        p["explanation"] = st.text_area("原来的程序为什么会失败?说清楚程序状态,而不只是你改了什么。", value=p["explanation"], height=110,
                                        key=f"expl-{task['id']}")
        if st.button("检查我的解释"):
            if len(p["explanation"].split()) + len(p["explanation"]) // 6 < 8:
                st.warning("请至少写一两句完整的话。")
            elif diag:
                s_ = hyp_status(p["explanation"], diag)
                p["explFeedback"] = {"on_track": "你的解释点到了关键。再确认一下:失败时变量实际是什么值,是哪条 Python 规则导致的?",
                                     "partly": "你触及了原因。补上程序当时的状态(哪个变量、什么值)和对应的 Python 规则。",
                                     "off_track": "解释还没点出原因。按步骤描述程序做了什么,并和你的预期比较。"}[s_]
        if p["explFeedback"]:
            st.info(p["explFeedback"])

        # ---- step 6/7
        tr = task.get("transfer")
        if tr:
            st.markdown("### 第 6 步 · 迁移:同一原则,新代码")
            st.code(tr["code"], language="python", line_numbers=True)
            st.caption(tr["question"])
            p["transfer"]["prediction"] = st.text_input("运行前先预测", value=p["transfer"]["prediction"], key=f"tp-{task['id']}")
            if st.button("运行迁移题"):
                p["transfer"]["result"] = term_text(tr["call"], dispatch({"type": "run", "code": tr["code"], "call": tr["call"]}))
            if p["transfer"]["result"]:
                st.code(p["transfer"]["result"], language="text")
            p["transfer"]["why"] = st.text_area("这里适用哪条调试原则?", value=p["transfer"]["why"], height=70, key=f"tw-{task['id']}")
        st.markdown("### 第 7 步 · 反思")
        p["ref"]["a"] = st.text_area("你最初的猜测是什么?实验怎样改变了它?", value=p["ref"]["a"], height=70, key=f"ra-{task['id']}")
        p["ref"]["b"] = st.text_area("你需要了哪一级提示?没有它能找到 bug 吗?", value=p["ref"]["b"], height=70, key=f"rb-{task['id']}")
        p["ref"]["c"] = st.text_area("下次遇到类似 bug,你先检查什么?", value=p["ref"]["c"], height=70, key=f"rc-{task['id']}")

        # ---- record
        st.markdown("### 学习记录")
        name = st.text_input("你的名字", key="student_name")
        done = sum(map(bool, [p["hypotheses"], p["experiments"], p["hints"], p["tests"], p["explanation"], p["ref"]["a"]]))
        st.progress(done / 6, text=f"已完成 {done} / 6 项")
        if len(p["explanation"].split()) + len(p["explanation"]) // 6 < 8:
            add_flag(p, "thin_expl", "解释过短。") if p["explanation"] else None
        d1, d2 = st.columns(2)
        d1.download_button("下载 .json(老师看板可导入)", json.dumps(record_obj(p, task, name), ensure_ascii=False),
                           file_name=f"DebugWise_{task['id']}.json", mime="application/json", disabled=not name, use_container_width=True)
        d2.download_button("下载 .md", record_md(p, task, name), file_name=f"DebugWise_{task['id']}.md", disabled=not name, use_container_width=True)
        if not name:
            st.caption("填写名字后可下载。")


# ------------------------------------------------------------------ teacher page
def sample_records():
    t0 = now_ms() - 3 * 86_400_000
    def rec(name, task, hints, flags, passed, expl):
        tk = next(t for t in TASKS if t["id"] == task)
        return {"v": 1, "student": name, "taskId": task, "taskTitle": tk["title"], "sharedAt": t0, "code": tk["code"],
                "progress": {"startedAt": t0, "hypotheses": [{"text": "我认为循环范围越界,因为 range 多了 1", "at": t0 + 60_000}],
                             "experiments": [{"call": "", "target": "orig", "expect": "", "result": "IndexError", "note": "出现越界", "at": t0 + 120_000}],
                             "hints": [{"level": i + 1, "at": t0 + 200_000 * (i + 1), "payload": {"support": f"提示{i + 1}"}} for i in range(hints)],
                             "tests": [{"call": "c", "expect": "e", "got": "g", "pass": i < passed} for i in range(3)],
                             "explanation": expl, "ref": {"a": "", "b": "", "c": ""}, "flags": [{"type": f, "msg": "", "at": t0} for f in flags],
                             "diagnosis": {"category": "示例", "confidence": 0.4 if "low_conf" in flags else 0.9}, "revised": ""}}
    return [rec("示例·小明", "longest-word", 1, [], 3, "range 多一位导致越界,最后一次访问了不存在的下标。"),
            rec("示例·小红", "class-average", 3, ["rapid", "early_fix"], 3, "改了"),
            rec("示例·小刚", "study-minutes", 2, ["low_conf"], 2, "类型不对,字符串不能直接和数字相加。"),
            rec("示例·小李", "has-negative", 3, ["off_track_fix"], 3, "加了个 flag")]


def teacher_page():
    st.subheader("教师看板")
    st.caption("上传学生下载的学习记录 .json(网页版导出的也可以)。数据只在本次会话内存里,不会保存到服务器。")
    up = st.file_uploader("学习记录 .json(可多选)", type="json", accept_multiple_files=True)
    recs, bad = [], 0
    for f in up or []:
        try:
            o = json.loads(f.read())
            assert o["progress"]["hypotheses"] is not None and o["taskTitle"]
            recs.append(o)
        except Exception:
            bad += 1
    if bad:
        st.warning(f"{bad} 个文件不是有效的 DebugWise 记录。")
    if not recs:
        if st.toggle("使用示例数据预览看板", value=not up):
            recs = sample_records()
        else:
            return
    rows = []
    for r in recs:
        pg = r["progress"]
        tests = pg.get("tests") or []
        rows.append({"学生": r["student"], "任务": r["taskTitle"], "提示级别": len(pg["hints"]), "假设数": len(pg["hypotheses"]),
                     "实验数": len(pg["experiments"]), "测试通过": f"{sum(t['pass'] for t in tests)}/{len(tests)}" if tests else "–",
                     "解释词数": len(pg.get("explanation", "").split()) + len(pg.get("explanation", "")) // 6,
                     "标记": "、".join(FLAG_LABEL.get(f["type"], f["type"]) for f in pg["flags"])})
    df = pd.DataFrame(rows)
    flagged = sum(1 for r in recs if r["progress"]["flags"])
    m = st.columns(4)
    m[0].metric("记录数", len(recs))
    m[1].metric("需要复核", flagged)
    m[2].metric("低置信度诊断", sum(any(f["type"] == "low_conf" for f in r["progress"]["flags"]) for r in recs))
    m[3].metric("平均提示级别", f"{df['提示级别'].mean():.1f} / 3")
    c1, c2 = st.columns(2)
    c1.markdown("**提示级别分布**")
    c1.bar_chart(df["提示级别"].value_counts().reindex([0, 1, 2, 3], fill_value=0).rename_axis("级别"))
    flags = pd.Series([FLAG_LABEL.get(f["type"], f["type"]) for r in recs for f in r["progress"]["flags"]], dtype=str)
    c2.markdown("**风险标记**")
    c2.bar_chart(flags.value_counts()) if len(flags) else c2.caption("没有标记。")
    st.markdown("**各任务平均提示级别**")
    st.bar_chart(df.groupby("任务")["提示级别"].mean())
    only = st.checkbox("只看有标记的")
    view = df[df["标记"] != ""] if only else df
    st.dataframe(view, hide_index=True, use_container_width=True)
    if len(view):
        pick = st.selectbox("查看某条记录的过程", view.index, format_func=lambda i: f"{df.loc[i, '学生']} · {df.loc[i, '任务']}")
        pg = recs[pick]["progress"]
        ev = [(h["at"], "假设", h["text"]) for h in pg["hypotheses"]]
        ev += [(x["at"], "实验", f"{x.get('call') or '运行程序'} → {x['result']};{x['note']}") for x in pg["experiments"]]
        ev += [(h["at"], f"提示 L{h['level']}", h["payload"].get("support") or h["payload"].get("fix_lines") or "") for h in pg["hints"]]
        t0 = pg.get("startedAt") or min(e[0] for e in ev) if ev else 0
        for at, kind, txt in sorted(ev):
            s = max(0, round((at - t0) / 1000))
            st.markdown(f"`{s // 60}:{s % 60:02d}` **{kind}** {txt}")
        st.markdown("**解释** " + (pg.get("explanation") or "_未填写_"))
        for f in pg["flags"]:
            st.warning(f"{FLAG_LABEL.get(f['type'], f['type'])}:{f.get('msg', '')}")


# ------------------------------------------------------------------ shell
st.sidebar.title("🐞 DebugWise")
st.sidebar.caption("先诊断,再修复")
page = st.sidebar.radio("视图", ["学生", "教师看板"], label_visibility="collapsed")
st.sidebar.markdown("---")
st.sidebar.caption("代码在受限的子进程里运行(7 秒超时)。除你主动下载或上传的文件外,不会保存任何数据。")
student_page() if page == "学生" else teacher_page()
