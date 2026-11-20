"""DupeScope 技能统一入口（薄封装）。

设计原则：
  1. 不硬编码用户工作区路径——引擎随技能自带（scripts/engine/），到哪都能跑；
  2. 二进制（ffmpeg/rgain 约 300 MB）不进技能包，多级探测本机已有，缺失给免费下载指引；
  3. 数据（缓存/报告）落在 ~/.dupe-audio-data/，不写脏技能目录，升级技能不丢数据；
  4. 每个子命令都可直接被 AI 用 Bash 调用，输出 JSON 或人类可读文本。

用法：
  python launcher.py doctor                      环境体检
  python launcher.py analyze <目录> [目录...]    版本甄别（同曲多版本）
  python launcher.py order  <目录>              播放顺序方案
  python launcher.py align  <目录>              位对齐（AccurateRip）
  python launcher.py gain   <目录>              响度扫描（只读）
  python launcher.py serve  [--port 8765]       起本地网页应用
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
ENGINE = SKILL / "scripts" / "engine"
DATA = Path(os.environ.get("DUPE_AUDIO_DATA") or (Path.home() / ".dupe-audio-data"))
CREATE_NO_WINDOW = 0x08000000

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/"       # essentials build，免费
RSGAIN_URL = "https://github.com/complexlogic/rsgain/releases"  # GPL，免费
CUETOOLS_URL = "https://cue.tools/wiki/CUETools_Download"


def _py() -> str:
    """选一个带依赖的 Python：当前解释器优先（AI 用 venv python 调就是它）。"""
    return sys.executable


def setup_env() -> dict:
    """把引擎路径注入 sys.path，探测本机工具并注入环境变量，返回环境摘要。

    工具探测结果与 DUPE_HOME 解耦：ffmpeg 可能在工作区副本、技能 data 目录或 PATH 里，
    探测到就通过 DUPE_FFMPEG / DUPE_FFPROBE / DUPE_RSGAIN 直接告诉引擎，
    这样技能副本引擎能复用本机已下载的 300MB 二进制，无需重复下载。
    """
    sys.path.insert(0, str(ENGINE))
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "reports").mkdir(exist_ok=True)
    os.environ["DUPE_HOME"] = str(DATA)
    ff = find_tool("ffmpeg")
    fp = find_tool("ffprobe")
    rg = find_tool("rsgain")
    if ff:
        os.environ["DUPE_FFMPEG"] = ff
    if fp:
        os.environ["DUPE_FFPROBE"] = fp
    if rg:
        os.environ["DUPE_RSGAIN"] = rg
    return {"engine": str(ENGINE), "data": str(DATA), "python": _py(),
            "ffmpeg": ff, "rsgain": rg}


def find_tool(name: str, extra: list[str] | None = None) -> str | None:
    """多级探测：技能 data/tools -> 工作区常见位置 -> PATH。"""
    cands = list(DATA.glob(f"tools/**/{name}.exe")) + list(DATA.glob(f"tools/{name}.exe"))
    for base in (Path.home() / "WorkBuddy", Path.home() / "workbuddy"):
        if base.is_dir():
            cands += list(base.glob(f"*/dupescope/tools/**/{name}.exe"))
    cands += [Path(x) for x in (extra or [])]
    for c in cands:
        if c.is_file():
            return str(c)
    return shutil.which(name)


def _run_module(module: str, args: list[str]) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ENGINE) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([_py(), "-m", module, *args], env=env,
                          creationflags=CREATE_NO_WINDOW).returncode


def cmd_doctor(_args: list[str]) -> int:
    setup_env()
    py = _py()
    deps = {}
    for mod in ("numpy", "rapidfuzz", "mutagen", "pypinyin"):
        r = subprocess.run([py, "-c", f"import {mod}; print('ok')"],
                           capture_output=True, text=True, timeout=60,
                           creationflags=CREATE_NO_WINDOW)
        deps[mod] = r.returncode == 0
    ff = find_tool("ffmpeg")
    rg = find_tool("rsgain")
    out = {
        "engine": str(ENGINE), "data": str(DATA), "python": py,
        "python_deps": deps,
        "ffmpeg": ff or "MISSING",
        "rsgain": rg or "MISSING",
        "ready": all(deps.values()) and bool(ff),
        "hints": [],
    }
    if not all(deps.values()):
        miss = " ".join(k for k, v in deps.items() if not v)
        out["hints"].append(f"缺 Python 依赖：{miss}。安装：pip install {miss}")
    if not ff:
        out["hints"].append(f"未找到 ffmpeg。免费下载 essentials build：{FFMPEG_URL}；"
                            f"解压后把 ffmpeg.exe 放到 {DATA / 'tools' / 'bin'}/")
    if not rg:
        out["hints"].append(f"未找到 rsgain（响度归一功能需要，其它功能不受影响）。"
                            f"免费 GPL：{RSGAIN_URL}；解压到 {DATA / 'tools' / 'rsgain'}/")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ready"] else 1


def cmd_analyze(args: list[str]) -> int:
    """同曲多版本甄别。用引擎的 pipeline 直接跑，输出 JSON 结果路径。"""
    if not args:
        print(json.dumps({"error": "用法：launcher.py analyze <目录> [目录...]"},
                         ensure_ascii=False))
        return 2
    setup_env()
    from dsc import pipeline, report  # noqa: PLC0415
    res = pipeline.run(roots=args, workers=4)
    if res.get("error") and not res.get("groups"):
        print(json.dumps(res, ensure_ascii=False))
        return 1
    out = report.write_reports(res, DATA / "reports", "skill")
    brief = {
        "ok": True,
        "stats": res["stats"],
        "groups": [{
            "title": g["title"],
            "versions": [{"rank": v["rank"], "name": v["name"], "path": v["path"],
                          "score": v.get("score"), "cutoff_khz": v.get("cutoff_khz"),
                          "dr": v.get("dr_value"), "flags": [f["code"] for f in v.get("flags", [])]}
                         for v in g["versions"]],
            "advice": g.get("advice"),
        } for g in res["groups"] if len(g["versions"]) > 1],
        "report_html": out.get("html"), "report_csv": out.get("csv"),
    }
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    return 0


def cmd_order(args: list[str]) -> int:
    if not args:
        print(json.dumps({"error": "用法：launcher.py order <目录>"}, ensure_ascii=False))
        return 2
    setup_env()
    from dsc import order as od  # noqa: PLC0415
    plan = od.build_plan(args[0], sort_by="title")
    print(json.dumps({"ok": True, "pinyin": od.pinyin_available(),
                      "files": [{"index": f["index"], "new_name": f["new_name"],
                                 "artist": f.get("artist")} for f in plan["files"]],
                      "note": "只出方案，未改动文件；应用请用 --apply 或网页版"},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_align(args: list[str]) -> int:
    if not args:
        print(json.dumps({"error": "用法：launcher.py align <目录> [--offline]"},
                         ensure_ascii=False))
        return 2
    setup_env()
    from dsc import accuracy as ac  # noqa: PLC0415
    fetch = "--offline" not in args
    cues = ac.find_cues([args[0]])
    if not cues:
        print(json.dumps({"status": "need_cue",
                          "hint": "没找到 .cue。AccurateRip 需整盘 cue + 原始抓轨；单曲文件物理上无法校验。"},
                         ensure_ascii=False, indent=2))
        return 0
    results = []
    for c in cues:
        try:
            results.append(ac.verify_cue(c, fetch=fetch))
        except Exception as e:  # noqa: BLE001
            results.append({"cue": c, "status": "error", "reason": str(e)[:160]})
    print(json.dumps({"count": len(cues), "results": results}, ensure_ascii=False, indent=2))
    return 0


def cmd_gain(args: list[str]) -> int:
    if not args:
        print(json.dumps({"error": "用法：launcher.py gain <目录>"}, ensure_ascii=False))
        return 2
    setup_env()
    from dsc import gain as gn  # noqa: PLC0415
    r = gn.plan(args[0])
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if not r.get("error") else 1


def cmd_serve(args: list[str]) -> int:
    setup_env()  # 探测工具并写入 os.environ，下面的子进程继承
    port = 8765
    if "--port" in args:
        try:
            port = int(args[args.index("--port") + 1])
        except (ValueError, IndexError):
            pass
    env = dict(os.environ)
    env["DUPE_HOME"] = str(DATA)
    env["PYTHONPATH"] = str(ENGINE) + os.pathsep + env.get("PYTHONPATH", "")
    import webbrowser
    import threading
    url = f"http://127.0.0.1:{port}/"
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"DupeScope 启动 -> {url}（关掉窗口即停止）", flush=True)
    return subprocess.run([_py(), str(ENGINE / "server.py"), "--port", str(port)],
                          env=env, creationflags=CREATE_NO_WINDOW).returncode


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "缺子命令", "commands":
                          ["doctor", "analyze", "order", "align", "gain", "serve"]},
                         ensure_ascii=False))
        return 2
    cmd, args = sys.argv[1], sys.argv[2:]
    return {
        "doctor": cmd_doctor, "analyze": cmd_analyze, "order": cmd_order,
        "align": cmd_align, "gain": cmd_gain, "serve": cmd_serve,
    }.get(cmd, lambda _a: print(json.dumps({"error": f"未知子命令 {cmd}"},
                                           ensure_ascii=False)) or 2)(args)


if __name__ == "__main__":
    sys.exit(main())
