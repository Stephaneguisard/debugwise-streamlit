from streamlit.testing.v1 import AppTest

def test_student_flow():
    at = AppTest.from_file("streamlit_app.py", default_timeout=60).run()
    assert not at.exception, at.exception
    # hypothesis
    [t for t in at.text_area if t.label == "你的假设"][0].set_value("我认为 bug 在第 3 行 range(len(words) + 1) 越界,因为下标会超出列表")
    [b for b in at.button if b.label == "保存假设"][0].click().run()
    assert not at.exception
    # experiment
    [t for t in at.text_input if t.label.startswith("调用")][0].set_value('longest_word(["a","bb"])').run()
    [b for b in at.button if b.label == "运行实验"][0].click().run()
    assert any("IndexError" in c.value for c in at.code), [c.value for c in at.code]
    [t for t in at.text_area if t.label.startswith("这个结果")][0].set_value("出现了 IndexError 说明下标越界").run()
    [b for b in at.button if b.label == "记录实验"][0].click().run()
    # unlock L1
    [b for b in at.button if b.label.startswith("解锁第 1")][0].click().run()
    assert not at.exception
    assert any("Off-by-one" in m.value for m in at.markdown), "L1 hint missing"
    # L2 locked until new hypothesis+experiment
    assert [b for b in at.button if b.label.startswith("解锁第 2")][0].disabled

def test_teacher_sample():
    at = AppTest.from_file("streamlit_app.py", default_timeout=60).run()
    at.sidebar.radio[0].set_value("教师看板").run()
    assert not at.exception, at.exception
    assert len(at.metric) == 4 and at.metric[0].value == "4"
