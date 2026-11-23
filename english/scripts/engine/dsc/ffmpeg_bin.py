"""定位 ffmpeg / ffprobe / Spek。优先用随包 tools 目录，其次 PATH，其次常见安装位置。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

# DUPE_HOME 可把"工具/缓存/报告"重定向到别处（技能副本复用本机工具时用）。
# 默认仍是源码所在项目根，行为与独立运行完全一致。
HOME = Path(os.environ.get("DUPE_HOME") or Path(__file__).resolve().parent.parent)
TOOLS = HOME / "tools"


def _first(*cands: str | Path) -> str | None:
    for c in cands:
        if not c:
            continue
        p = Path(c)
        if p.is_file():
            return str(p)
        w = shutil.which(str(c))
        if w:
            return w
    return None


def ffmpeg() -> str:
    return (os.environ.get("DUPE_FFMPEG")
            or _first(TOOLS / "bin" / "ffmpeg.exe", "ffmpeg", "ffmpeg.exe") or "ffmpeg")


def ffprobe() -> str:
    return (os.environ.get("DUPE_FFPROBE")
            or _first(TOOLS / "bin" / "ffprobe.exe", "ffprobe", "ffprobe.exe") or "ffprobe")


def spek() -> str | None:
    return _first(
        r"C:\Program Files\Spek\spek.exe",
        r"C:\Program Files (x86)\Spek\spek.exe",
        "spek",
        "spek.exe",
    )


def explorer() -> str:
    return str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe")


def cuetools() -> str | None:
    """探测 CUETools（AccurateRip/CTDB GUI 复核入口）。不随包，用户自装。"""
    return _first(
        r"C:\Program Files\CUETools\CUETools.exe",
        r"C:\Program Files (x86)\CUETools\CUETools.exe",
        r"C:\Program Files\CUETools 2\CUETools.exe",
        "CUETools.exe",
    )


CUETOOLS_DOWNLOAD = "https://cue.tools/wiki/CUETools_Download"


def check() -> dict:
    """自检：返回各外部依赖的可用状态（探活必须实跑，不能只看有没有文件）。"""
    import subprocess

    out = {}
    for name, exe, args in (
        ("ffmpeg", ffmpeg(), ["-version"]),
        ("ffprobe", ffprobe(), ["-version"]),
    ):
        try:
            r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=20,
                               creationflags=0x08000000)
            line = (r.stdout or r.stderr).splitlines()[0] if (r.stdout or r.stderr) else ""
            out[name] = {"ok": r.returncode == 0, "path": exe, "version": line.strip()[:80]}
        except Exception as e:  # noqa: BLE001
            out[name] = {"ok": False, "path": exe, "error": str(e)[:160]}

    sp = spek()
    out["spek"] = {"ok": bool(sp), "path": sp or "", "note": "仅 GUI，无命令行参数；用于目视复核"}
    ct = cuetools()
    out["cuetools"] = {"ok": bool(ct), "path": ct or "",
                       "note": "AccurateRip/CTDB GUI 复核；位对齐本工具已内置自动实现"}
    try:
        from . import gain  # noqa: PLC0415
        out["rsgain"] = gain.check()
    except Exception:  # noqa: BLE001
        out["rsgain"] = {"ok": False}
    return out
