# DebugWise · Streamlit 版

Python 调试辅导的 Streamlit 版本:学生按"假设 → 实验 → 分级提示 → 修改测试 → 解释 → 迁移 → 反思"的流程调试代码;老师把学生下载的学习记录(.json)拖进"教师看板"查看统计和过程。网页版导出的 .json 也能导入。

## 本地运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/streamlit run streamlit_app.py
.venv/bin/pytest -q          # 测试
```

## 免费部署到 xxx.streamlit.app

1. 打开 <https://share.streamlit.io>,用 GitHub 账号登录并授权访问本仓库。
2. 点 **Create app → Deploy a public app from GitHub**,选择本仓库、分支 `main`、主文件 `streamlit_app.py`。
3. 在 **App URL** 里填想要的子域名(如 `debugwise`),点 Deploy,约 2 分钟后上线。

## 文件

| 文件 | 作用 |
|---|---|
| `streamlit_app.py` | 界面与流程(学生页、教师看板) |
| `engine.py` | 诊断引擎(与网页版同一份,纯 Python:`ast` 静态分析 + 运行时异常识别) |
| `sandbox.py` / `runner.py` | 在独立子进程中运行学生代码:7 秒超时、CPU/内存/文件大小限制、清空环境变量 |
| `tasks.json` | 4 个内置任务(从网页版提取) |
| `make_qr.py` | 为任意网址生成二维码 PNG |

## 安全说明

学生代码在服务器上执行。这里做了子进程隔离、超时和资源限制,但**不是强沙箱**:代码仍可读取容器内文件、发起网络请求。适合课堂演示;公开长期运行建议改用网页版(Pyodide 在学生浏览器里运行)或容器级隔离。应用不保存任何学生数据,记录只通过用户主动下载/上传流转。
