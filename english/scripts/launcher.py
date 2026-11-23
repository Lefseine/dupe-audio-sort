"""DupeScope skill unified entry point (thin wrapper).

Design principles:
  1. No hardcoded user workspace paths — the engine ships with the skill (scripts/engine/) and runs anywhere;
  2. Binaries (ffmpeg/rgain ~300 MB) are not bundled in the skill package; multi-level probing finds local copies and gives free download guidance when missing;
  3. Data (cache/reports) lives in ~/.dupe-audio-data/ — never pollutes the skill directory, so upgrading the skill never loses data;
  4. Every subcommand can be called directly by AI via Bash, emitting JSON or human-readable text.

Usage:
  python launcher.py doctor                      environment check
  python launcher.py analyze <dir> [dir...]      version identification (same song, multiple versions)
  python launcher.py order  <dir>                playback order plan
  python launcher.py align  <dir>                bit-perfect alignment (AccurateRip)
  python launcher.py gain   <dir>                loudness scan (read-only)
  python launcher.py serve  [--port 8765]        start the local web app
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

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/"       # essentials build, free
RSGAIN_URL = "https://github.com/complexlogic/rsgain/releases"  # GPL, free
CUETOOLS_URL = "https://cue.tools/wiki/CUETools_Download"


def _py() -> str:
    """Pick a Python with the dependencies installed: current interpreter first (when AI calls with a venv python, that's the one)."""
    return sys.executable


def setup_env() -> dict:
    """Inject the engine path into sys.path, probe local tools and inject env vars, return an environment summary.

    Tool probing is decoupled from DUPE_HOME: ffmpeg may live in a workspace copy, the skill data dir, or PATH;
    whenever found we tell the engine directly via DUPE_FFMPEG / DUPE_FFPROBE / DUPE_RSGAIN,
    so the skill's bundled engine can reuse locally downloaded 300MB binaries with no re-download.
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
    """Multi-level probing: skill data/tools -> common workspace locations -> PATH."""
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
        out["hints"].append(f"Missing Python dependencies: {miss}. Install: pip install {miss}")
    if not ff:
        out["hints"].append(f"ffmpeg not found. Free essentials build: {FFMPEG_URL}; "
                            f"unzip and place ffmpeg.exe in {DATA / 'tools' / 'bin'}/")
    if not rg:
        out["hints"].append(f"rsgain not found (required for loudness normalization; other features are unaffected). "
                            f"Free GPL build: {RSGAIN_URL}; unzip into {DATA / 'tools' / 'rsgain'}/")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ready"] else 1


def cmd_analyze(args: list[str]) -> int:
    """Identify multiple versions of the same song. Runs the engine's pipeline directly and emits a JSON result path."""
    if not args:
        print(json.dumps({"error": "Usage: launcher.py analyze <dir> [dir...]"},
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
        print(json.dumps({"error": "Usage: launcher.py order <dir>"}, ensure_ascii=False))
        return 2
    setup_env()
    from dsc import order as od  # noqa: PLC0415
    plan = od.build_plan(args[0], sort_by="title")
    print(json.dumps({"ok": True, "pinyin": od.pinyin_available(),
                      "files": [{"index": f["index"], "new_name": f["new_name"],
                                 "artist": f.get("artist")} for f in plan["files"]],
                      "note": "Plan only; no files changed. Use --apply or the web UI to apply"},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_align(args: list[str]) -> int:
    if not args:
        print(json.dumps({"error": "Usage: launcher.py align <dir> [--offline]"},
                         ensure_ascii=False))
        return 2
    setup_env()
    from dsc import accuracy as ac  # noqa: PLC0415
    fetch = "--offline" not in args
    cues = ac.find_cues([args[0]])
    if not cues:
        print(json.dumps({"status": "need_cue",
                          "hint": "No .cue found. AccurateRip requires a full-disc cue + original rips; single-track files cannot be verified physically."},
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
        print(json.dumps({"error": "Usage: launcher.py gain <dir>"}, ensure_ascii=False))
        return 2
    setup_env()
    from dsc import gain as gn  # noqa: PLC0415
    r = gn.plan(args[0])
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if not r.get("error") else 1


def cmd_serve(args: list[str]) -> int:
    setup_env()  # probe tools and write into os.environ; the child process below inherits them
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
    print(f"DupeScope started -> {url} (close the window to stop)", flush=True)
    return subprocess.run([_py(), str(ENGINE / "server.py"), "--port", str(port)],
                          env=env, creationflags=CREATE_NO_WINDOW).returncode


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Missing subcommand", "commands":
                          ["doctor", "analyze", "order", "align", "gain", "serve"]},
                         ensure_ascii=False))
        return 2
    cmd, args = sys.argv[1], sys.argv[2:]
    return {
        "doctor": cmd_doctor, "analyze": cmd_analyze, "order": cmd_order,
        "align": cmd_align, "gain": cmd_gain, "serve": cmd_serve,
    }.get(cmd, lambda _a: print(json.dumps({"error": f"Unknown subcommand {cmd}"},
                                           ensure_ascii=False)) or 2)(args)


if __name__ == "__main__":
    sys.exit(main())
