"""本地服务：把引擎包成一个小网页应用。只用标准库，双击 run.cmd 就能跑。

    python server.py            # 默认 http://127.0.0.1:8765
    python server.py --port 9000
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dsc import accuracy as ac  # noqa: E402
from dsc import gain as gn  # noqa: E402
from dsc import ffmpeg_bin, pipeline, report  # noqa: E402
from dsc import order as od  # noqa: E402

ROOT = Path(__file__).resolve().parent        # 源码根：web 页面随包走
WEB = ROOT / "web"
DATA = Path(os.environ.get("DUPE_HOME") or ROOT)  # 数据根：报告/缓存可被技能重定向
JOBS: dict[str, dict] = {}
_CREATE_NO_WINDOW = 0x08000000
_RECENT = DATA / ".dupescope" / "recent.json"


def _recent_paths() -> list[str]:
    """记住最近分析过的根目录，供「拖入文件夹反查路径」优先使用。"""
    try:
        v = json.loads(_RECENT.read_text(encoding="utf-8"))
        return [x for x in v if isinstance(x, str)][:20]
    except Exception:  # noqa: BLE001
        return []


def _remember(paths) -> None:
    try:
        merged: list[str] = []
        for p in list(paths or []) + _recent_paths():
            s = str(p)
            if s not in merged:
                merged.append(s)
        _RECENT.parent.mkdir(parents=True, exist_ok=True)
        _RECENT.write_text(json.dumps(merged[:20], ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _resolve_folder(name: str) -> list[str]:
    """浏览器拖入文件夹只能拿到名字；据此在合理范围内浅层反查绝对路径。

    最近用过的目录优先，其次是家目录 / Music / 各盘符的前两层。只做浅层扫描，
    跳过系统目录，找到即停，避免全盘遍历拖慢界面。找不到时前端会回退到原生对话框。
    """
    raw = (name or "").strip().strip('"').strip("/\\")
    target = raw.split("/")[-1].split("\\")[-1].casefold()
    if not target or target in (".", ".."):
        return []

    found: list[str] = []
    seen: set[str] = set()

    def consider(p: Path) -> None:
        if p.name.casefold() != target:
            return
        key = str(p).casefold()
        if key in seen:
            return
        try:
            if not p.is_dir():
                return
        except OSError:
            return
        seen.add(key)
        found.append(str(p))

    starts: list[tuple[Path, int]] = []
    for s in _recent_paths():
        pp = Path(s)
        if pp.is_dir():
            starts.append((pp.parent, 3))
    h = Path.home()
    starts += [(h, 2), (h / "Music", 3), (h / "音乐", 3)]
    skip = {"AppData", "Windows", "Program Files", "Program Files (x86)",
            "node_modules", "__pycache__", "$Recycle.Bin", "System Volume Information"}
    for drv in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        d = Path(f"{drv}:\\")
        if d.exists():
            starts.append((d, 2))

    for base, maxdepth in starts:
        if found:
            break
        try:
            base = base.resolve()
        except OSError:
            continue
        consider(base)
        if found:
            break
        visited = 0
        for cur, dirs, _ in os.walk(base):
            try:
                depth = len(Path(cur).relative_to(base).parts)
            except ValueError:
                break
            if depth >= maxdepth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs
                       if d not in skip and not d.startswith((".", "$", "~"))]
            for d in dirs:
                consider(Path(cur) / d)
                if found:
                    break
            visited += 1
            if found or visited > 2500:
                break
    return found[:8]


def _new_job() -> dict:
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {
        "id": jid,
        "state": "queued",
        "created": time.time(),
        "progress": {"phase": "Queued", "done": 0, "total": 0, "current": ""},
        "result": None,
        "error": None,
        "cancel": False,
    }
    return JOBS[jid]


def _run_job(job: dict, opts: dict) -> None:
    job["state"] = "running"
    _remember(opts.get("roots") or [])

    def on_progress(p: dict) -> None:
        job["progress"] = p

    try:
        res = pipeline.run(
            roots=opts["roots"],
            on_progress=on_progress,
            workers=opts.get("workers"),
            only_multi_version=opts.get("only_multi", True),
            max_files=int(opts.get("max_files") or 0),
            use_cache=opts.get("use_cache", True),
            job=job,
        )
        if res.get("error") and not res.get("groups"):
            job["state"] = "error"
            job["error"] = res["error"]
        else:
            job["result"] = res
            job["state"] = "done"
            report.write_reports(res, DATA / "reports", job["id"])
    except Exception as e:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = f"{e}"
        job["trace"] = traceback.format_exc()[-2000:]


class Handler(BaseHTTPRequestHandler):
    server_version = "DupeScope/1.0"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):  # 静音
        pass

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        if not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # ---------- 路由 ----------
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)

        if p in ("/", "/index.html"):
            return self._file(WEB / "index.html")
        if p.startswith("/static/"):
            return self._file(WEB / p[len("/static/"):])
        if p == "/api/health":
            return self._json({"ok": True, "tools": ffmpeg_bin.check(), "version": "1.4.0",
                               "order": {"pinyin": od.pinyin_available(),
                                         "sort_modes": od.SORT_MODES}})
        if p == "/api/order/manifests":
            folder = (q.get("path") or [""])[0]
            if not folder or not Path(folder).is_dir():
                return self._json({"error": "Path does not exist"}, 400)
            return self._json({"items": od.list_manifests(folder)})
        if p == "/api/align/find":
            # 找 cue 文件（位对齐前置条件探测）
            folder = (q.get("path") or [""])[0]
            if not folder or not Path(folder).exists():
                return self._json({"error": "Path does not exist"}, 400)
            cues = ac.find_cues([folder])
            return self._json({"cues": cues, "count": len(cues),
                               "hint": ("Found cue; AccurateRip bit-perfect verification is available" if cues else
                                        "No .cue file found. Bit-perfect verification needs a full-disc cue + original rip; "
                                        "Single-track files have no TOC, so verification is physically impossible.")})
        if p == "/api/align/status":
            ct = ffmpeg_bin.cuetools()
            return self._json({"cuetools": bool(ct), "cuetools_path": ct or "",
                               "download": ffmpeg_bin.CUETOOLS_DOWNLOAD,
                               "note": "Automatic bit-perfect verification is built in; CUETools is only for GUI visual review"})
        if p == "/api/gain/status":
            return self._json(gn.check())
        if p == "/api/resolve-folder":
            # 拖拽文件夹时浏览器只给名字，这里在合理范围内浅层反查绝对路径
            name = (q.get("name") or [""])[0]
            hits = _resolve_folder(name)
            return self._json({"name": name, "paths": hits, "count": len(hits),
                               "hint": "" if hits else "Could not locate it; click Choose Folder to select manually"})
        if p == "/api/drives":
            drives = []
            for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                r = f"{d}:\\"
                if os.path.exists(r):
                    drives.append(r)
            home = str(Path.home())
            music = str(Path.home() / "Music")
            return self._json({"drives": drives, "home": home,
                               "music": music if Path(music).exists() else home})
        if p == "/api/browse":
            target = (q.get("path") or [""])[0].strip() or str(Path.home())
            try:
                t = Path(target)
                dirs = [{"name": c.name, "path": str(c)} for c in sorted(t.iterdir())
                        if c.is_dir() and not c.name.startswith((".", "$"))]
                auds = sum(1 for c in t.rglob("*") if c.is_file() and c.suffix.lower() in
                           {".flac", ".wav", ".ape", ".dsf", ".dff", ".m4a", ".mp3", ".aif", ".wv"})
                return self._json({"path": str(t), "parent": str(t.parent), "dirs": dirs[:400],
                                   "audio_count": auds})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 400)
        if p.startswith("/api/job/"):
            jid = p.rsplit("/", 1)[-1]
            j = JOBS.get(jid)
            if not j:
                return self._json({"error": "job not found"}, 404)
            return self._json({"id": j["id"], "state": j["state"], "progress": j["progress"],
                               "error": j["error"],
                               "stats": (j["result"] or {}).get("stats")})
        if p.startswith("/api/result/"):
            jid = p.rsplit("/", 1)[-1]
            j = JOBS.get(jid)
            if not j or not j["result"]:
                return self._json({"error": "not ready"}, 404)
            return self._json(j["result"])
        if p.startswith("/api/export/"):
            jid = p.rsplit("/", 1)[-1]
            fmt = (q.get("fmt") or ["html"])[0]
            f = DATA / "reports" / f"{jid}.{fmt}"
            return self._file(f)
        if p.startswith("/api/file"):
            # 本地文件播放/下载（只读音频）
            fp = (q.get("path") or [""])[0]
            return self._file(Path(fp))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        body = self._body()

        if u.path == "/api/scan":
            roots = [r for r in (body.get("roots") or []) if r and Path(r).exists()]
            if not roots:
                return self._json({"error": "Path does not exist"}, 400)
            job = _new_job()
            threading.Thread(target=_run_job, args=(job, {**body, "roots": roots}),
                             daemon=True).start()
            return self._json({"job_id": job["id"]})

        if u.path == "/api/order/scan":
            folder = body.get("path") or ""
            if not Path(folder).is_dir():
                return self._json({"error": "Path does not exist"}, 400)
            return self._json(od.scan(folder))

        if u.path == "/api/order/plan":
            folder = body.get("path") or ""
            if not Path(folder).is_dir():
                return self._json({"error": "Path does not exist"}, 400)
            return self._json(od.build_plan(
                folder,
                sort_by=body.get("sort_by") or "title",
                prefix=body.get("prefix") or "num",
                recursive=bool(body.get("recursive")),
            ))

        if u.path == "/api/order/apply":
            folder = body.get("path") or ""
            if not Path(folder).is_dir():
                return self._json({"error": "Path does not exist"}, 400)
            if not body.get("confirm"):
                return self._json({"error": "Second confirmation required (confirm=true)"}, 400)
            res = od.apply_plan(
                folder,
                sort_by=body.get("sort_by") or "title",
                prefix=body.get("prefix") or "num",
                actions=body.get("actions") or {},
                confirm=True,
                reverse_time=bool(body.get("reverse_time")),
            )
            return self._json(res)

        if u.path == "/api/order/undo":
            m = body.get("manifest") or ""
            return self._json(od.undo(m))

        if u.path == "/api/align/verify":
            cue = body.get("cue") or ""
            if not cue or not Path(cue).is_file():
                return self._json({"error": "cue file does not exist"}, 400)
            res = ac.verify_cue(cue, fetch=bool(body.get("fetch", True)))
            return self._json(res)

        if u.path == "/api/align/batch":
            # 对目录下全部 cue 批量校验：每张碟独立，失败不影响其它
            folder = body.get("path") or ""
            if not folder or not Path(folder).is_dir():
                return self._json({"error": "Path does not exist"}, 400)
            cues = ac.find_cues([folder])
            results = []
            for c in cues:
                try:
                    results.append(ac.verify_cue(c, fetch=bool(body.get("fetch", True))))
                except Exception as e:  # noqa: BLE001
                    results.append({"cue": c, "status": "error", "reason": str(e)[:160]})
            return self._json({"count": len(cues), "results": results})

        if u.path == "/api/gain/plan":
            folder = body.get("path") or ""
            if not folder or not Path(folder).is_dir():
                return self._json({"error": "Path does not exist"}, 400)
            return self._json(gn.plan(folder, int(body.get("lufs") or -18),
                                      bool(body.get("no_album"))))

        if u.path == "/api/gain/apply":
            folder = body.get("path") or ""
            if not folder or not Path(folder).is_dir():
                return self._json({"error": "Path does not exist"}, 400)
            if not body.get("confirm"):
                return self._json({"error": "Writing tags requires second confirmation (confirm=true)"}, 400)
            return self._json(gn.apply_tags(folder, int(body.get("lufs") or -18),
                                            bool(body.get("no_album"))))

        if u.path.startswith("/api/cancel/"):
            jid = u.path.rsplit("/", 1)[-1]
            if jid in JOBS:
                JOBS[jid]["cancel"] = True
            return self._json({"ok": True})

        if u.path == "/api/pick-folder":
            # 嵌入式 Python 没有 tkinter，所以优先用 PowerShell 的系统对话框
            try:
                ps = (
                    "Add-Type -AssemblyName System.Windows.Forms;"
                    "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
                    "$f.Description='Choose music folder';"
                    "$f.ShowNewFolderButton=$true;"
                    "if($f.ShowDialog() -eq 'OK'){Write-Output $f.SelectedPath}"
                )
                r = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-STA", "-Command", ps],
                    capture_output=True, text=True, timeout=600,
                    creationflags=_CREATE_NO_WINDOW,
                )
                picked = (r.stdout or "").strip().splitlines()
                if picked:
                    return self._json({"path": picked[-1].strip()})
            except Exception:  # noqa: BLE001
                pass
            try:
                code = (
                    "import tkinter as tk\n"
                    "from tkinter import filedialog\n"
                    "r=tk.Tk(); r.withdraw(); r.attributes('-topmost',True)\n"
                    "p=filedialog.askdirectory(title='Choose music folder')\n"
                    "print(p or '')\n"
                )
                r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                   text=True, timeout=300, creationflags=_CREATE_NO_WINDOW)
                return self._json({"path": (r.stdout or "").strip(),
                                   "hint": "If no window appears, click the Browse button in the interface"})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e),
                                   "hint": "Use the Browse directory tree in the interface instead"}, 500)

        if u.path == "/api/action":
            act = body.get("action")
            path = body.get("path") or ""
            if act == "spek":
                exe = ffmpeg_bin.spek()
                if not exe:
                    return self._json({"error": "Spek not found"}, 404)
                subprocess.Popen([exe, path], creationflags=_CREATE_NO_WINDOW)
                return self._json({"ok": True})
            if act == "cuetools":
                exe = ffmpeg_bin.cuetools()
                if not exe:
                    return self._json({"error": "CUETools not found",
                                       "download": ffmpeg_bin.CUETOOLS_DOWNLOAD}, 404)
                # /verify 触发 AccurateRip 自动校验；CUETools 不会自动关闭（官方行为）
                subprocess.Popen([exe, "/verify", path], creationflags=_CREATE_NO_WINDOW)
                return self._json({"ok": True,
                                   "note": "CUETools verification started; close it manually when done"})
            if act == "reveal" and path:
                subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(path)])
                return self._json({"ok": True})
            if act == "open" and path:
                os.startfile(path)  # noqa: S606
                return self._json({"ok": True})
            return self._json({"error": "Unknown action"}, 400)

        return self._json({"error": "not found"}, 404)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--order", metavar="DIR", help="Open Playback Order and scan the folder")
    ap.add_argument("--scan", metavar="DIR", help="Open Version Compare and scan the folder")
    a = ap.parse_args()

    url = f"http://{a.host}:{a.port}/"
    if a.order:
        url += "?tab=order&dir=" + quote(str(a.order))
    elif a.scan:
        url += "?auto=" + quote(str(a.scan))

    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError:
        # 已经有一个实例在跑：直接把这次的目录丢给它打开
        print(f"An instance is already running on port {a.port}; opening {url}")
        webbrowser.open(url)
        return

    print(f"DupeScope started -> {url}")
    print("Close this window to stop.")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
