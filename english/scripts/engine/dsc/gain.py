"""整库响度归一（ReplayGain）编排。

职责边界：调用随包 rsgain（GPL，免安装，只写标签不动音频流），
默认**只扫描不写标签**——把每个文件的 LUFS / 建议增益 / 峰值解析成可读建议，
用户确认后才写入。这样"响的显得好"的偏差可被量化，但文件保持可逆。

为什么不用自己的 ebur128 结果直接算增益：
  我们的 analyze.py 已算 LUFS-I，够做比较；但 rsgain 提供的是行业标准的
  ReplayGain 2.0 标签（能被 JRiver/foobar/手机播放器真正读取并自动应用），
  这是"写进文件、跨播放器生效"的能力，自算 LUFS 给不了。两者互补。
"""
from __future__ import annotations

import csv
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ffmpeg_bin

HOME = Path(os.environ.get("DUPE_HOME") or Path(__file__).resolve().parent.parent)
TOOLS = HOME / "tools"
_CREATE_NO_WINDOW = 0x08000000
DEFAULT_TARGET = -18          # ReplayGain 标准目标响度


def rsgain_bin() -> str | None:
    """定位 rsgain：环境变量 -> 随包 tools/rsgain/**/*.exe -> PATH -> scoop。"""
    env = os.environ.get("DUPE_RSGAIN")
    if env and Path(env).is_file():
        return env
    cands = list(TOOLS.glob("rsgain/**/rsgain.exe")) + [TOOLS / "rsgain" / "rsgain.exe"]
    for c in cands:
        if c.is_file():
            return str(c)
    w = shutil.which("rsgain")
    if w:
        return w
    scoop = Path.home() / "scoop" / "apps" / "rsgain"
    if scoop.is_dir():
        for c in scoop.rglob("rsgain.exe"):
            return str(c)
    return None


def check() -> dict:
    exe = rsgain_bin()
    if not exe:
        return {"ok": False, "path": "", "version": "",
                "note": "rsgain not found; extract the official portable package to tools/rsgain/ (free, GPL)"}
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=15, creationflags=_CREATE_NO_WINDOW)
        import re
        ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
        ver = re.sub(r"\x1b\[[0-9;]*m", "", ver).strip()   # 去 ANSI 色码
        return {"ok": r.returncode == 0, "path": exe, "version": ver[:60]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "path": exe, "error": str(e)[:160]}


def _csv_dir(log_dir: Path) -> Path:
    """rsgain easy 在每个扫描目录写 replaygain.csv；取最后一个非空的。"""
    found = list(log_dir.rglob("replaygain.csv"))
    if not found:
        return log_dir / "replaygain.csv"
    return max(found, key=lambda p: p.stat().st_mtime)


def plan(folder: str, target_lufs: int = DEFAULT_TARGET,
         no_album: bool = False) -> dict:
    """扫描文件夹，返回每曲的 ReplayGain 建议。不写任何标签、不改文件。

    rsgain 的 easy 模式本会写标签；为避免"扫描即改动"，我们在临时副本目录上跑，
    csv 只是扫描结果，与标签无关——所以原目录文件零改动。
    """
    exe = rsgain_bin()
    if not exe:
        return {"error": "rsgain not found", "items": [], "hint":
                "Download the win64 zip from https://github.com/complexlogic/rsgain/releases, "
                "extract to tools/rsgain/ and retry (free, GPL)"}
    src = Path(folder)
    if not src.is_dir():
        return {"error": "Path does not exist", "items": []}

    # 在临时目录建符号链接指向音频文件？Windows 需管理员权限建 symlink，改用过硬链接 copy
    # 但我们只需要 csv（扫描结果），可以安全地用 rsgain 直接扫原目录——
    # easy 模式会写 RG 标签。为避免改动，用 -s n（Custom 模式 tagmode=n 可能仍写）。
    # 最稳妥：复制音频到临时目录再扫。100GB 全库复制代价高，所以这里只复制"待比较组"的曲目，
    # 由调用方传入的 folder 通常是单个专辑/单曲集合，体积可控。
    with tempfile.TemporaryDirectory(prefix="rg_") as td:
        tmp = Path(td)
        n = 0
        for f in sorted(src.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in {".flac", ".wav", ".ape", ".wv",
                                                            ".mp3", ".m4a", ".ogg", ".opus", ".aif"}:
                continue
            dst = tmp / f"{n:04d}_{f.name}"
            shutil.copy2(f, dst)
            n += 1
        if n == 0:
            return {"error": "No scannable audio in this folder", "items": []}
        cmd = [exe, "easy", "-O", "-l", str(target_lufs)]
        if no_album:
            cmd += ["-p", "no_album"]
        cmd += [str(tmp)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                               creationflags=_CREATE_NO_WINDOW)
        except Exception as e:  # noqa: BLE001
            return {"error": f"rsgain run failed: {e}", "items": []}

        # 解析 stdout 摘要
        summary = {}
        for line in (r.stdout or "").splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip().replace(" ", "_")
                if k and v.strip():
                    summary[k] = v.strip()

        csv_path = _csv_dir(tmp)
        items = []
        if csv_path.is_file():
            with open(csv_path, encoding="utf-8-sig", errors="replace") as fh:
                reader = csv.DictReader(fh, delimiter="\t")
                # rsgain 的列名带前导空格（" Peak (dB)"），统一 strip
                reader.fieldnames = [(h or "").strip() for h in (reader.fieldnames or [])]
                for row in reader:
                    row = {(k or "").strip(): v for k, v in row.items()}
                    fn = next(iter(row.values()), "") or ""
                    if fn.strip().lower() == "album":
                        continue
                    items.append({
                        "name": fn,
                        "lufs": _num(row.get("Loudness (LUFS)")),
                        "gain_db": _num(row.get("Gain (dB)")),
                        "peak": _num(row.get("Peak")),
                        "peak_db": _num(row.get("Peak (dB)")),
                        "clip_adjust": row.get("Clipping Adjustment?"),
                    })
        # 把临时文件名映射回原文件名（04xx_ 前缀）
        for it, f in zip(items, sorted(p for p in src.rglob("*")
                                        if p.is_file() and p.suffix.lower() in
                                        {".flac", ".wav", ".ape", ".wv", ".mp3", ".m4a", ".ogg", ".opus", ".aif"})):
            it["source"] = str(f)
            it["name"] = f.name
        return {
            "items": items, "summary": summary, "target_lufs": target_lufs,
            "scanned": n, "writes_tags": False,
            "note": "Read-only scan (on a temp copy), original folder untouched; writing tags requires separate confirmation",
        }


def _num(s) -> float | None:
    try:
        return round(float(str(s).strip()), 2)
    except (ValueError, TypeError, AttributeError):
        return None


def apply_tags(folder: str, target_lufs: int = DEFAULT_TARGET,
               no_album: bool = False) -> dict:
    """用户确认后，直接在原目录写 ReplayGain 标签（rsgain 保证不动音频流）。

    可逆性：ReplayGain 标签可随时用 rsgain 重写或删除，音频数据不变。
    """
    exe = rsgain_bin()
    if not exe:
        return {"error": "rsgain not found"}
    src = Path(folder)
    if not src.is_dir():
        return {"error": "Path does not exist"}
    cmd = [exe, "easy", "-O", "-l", str(target_lufs)]
    if no_album:
        cmd += ["-p", "no_album"]
    cmd += [str(src)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                           creationflags=_CREATE_NO_WINDOW)
    except Exception as e:  # noqa: BLE001
        return {"error": f"rsgain run failed: {e}"}
    csv_path = _csv_dir(src)
    written = 0
    if csv_path.is_file():
        with open(csv_path, encoding="utf-8-sig", errors="replace") as fh:
            written = sum(1 for row in csv.DictReader(fh, delimiter="\t")
                          if (next(iter(row.values()), "") or "").strip().lower() != "album")
    return {"ok": r.returncode == 0, "tagged": written,
            "log": (r.stdout or "")[-1500:], "csv": str(csv_path),
            "note": "ReplayGain tags written; audio stream unchanged; can be rewritten with rsgain to undo"}


RSGAIN_DOWNLOAD = "https://github.com/complexlogic/rsgain/releases"
RSGAIN_DIRECT = "https://github.com/complexlogic/rsgain/releases/download/v3.8/rsgain-3.8-win64.zip"

if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--apply", action="store_true", help="Confirm writing tags (default: read-only scan)")
    ap.add_argument("--lufs", type=int, default=DEFAULT_TARGET)
    a = ap.parse_args()
    print(json.dumps(apply_tags(a.dir, a.lufs) if a.apply else plan(a.dir, a.lufs),
                     ensure_ascii=False, indent=2))
