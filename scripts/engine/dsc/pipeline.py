"""扫描编排：遍历 -> 探元数据 -> 分组 -> 只对"有多版"的歌做重分析（省时间）-> 排行。

缓存：以 (路径, 大小, mtime) 为键存 sqlite。100 GB 库里重扫时只有真的变过的文件会重算。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import analyze as an
from . import group as gp
from . import probe as pb

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
CACHE_DB = CACHE_DIR / "analysis.db"
_lock = threading.Lock()


class Cache:
    def __init__(self, path: Path = CACHE_DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, data TEXT)"
        )
        self.conn.commit()

    def get(self, path: str, size: int, mtime: float) -> dict | None:
        with _lock:
            row = self.conn.execute(
                "SELECT size,mtime,data FROM files WHERE path=?", (path,)
            ).fetchone()
        if row and row[0] == size and abs(row[1] - mtime) < 1e-6:
            try:
                return json.loads(row[2])
            except json.JSONDecodeError:
                return None
        return None

    def put(self, path: str, size: int, mtime: float, data: dict) -> None:
        with _lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO files(path,size,mtime,data) VALUES(?,?,?,?)",
                (path, size, mtime, json.dumps(data, ensure_ascii=False)),
            )
            self.conn.commit()


def collect_files(roots: list[str], skip_hidden: bool = True) -> list[str]:
    out: list[str] = []
    for r in roots:
        p = Path(r)
        if p.is_file() and pb.is_audio(p):
            out.append(str(p))
            continue
        if not p.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            if skip_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith((".", "$", "System Volume"))]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                fp = Path(dirpath) / fn
                if pb.is_audio(fp):
                    out.append(str(fp))
    return out


def run(
    roots: list[str],
    on_progress=None,
    workers: int | None = None,
    only_multi_version: bool = True,
    max_files: int = 0,
    use_cache: bool = True,
    job: dict | None = None,
) -> dict:
    job = job if job is not None else {}
    t0 = time.time()
    emit = on_progress or (lambda *_: None)
    cache = Cache() if use_cache else None

    emit({"phase": "扫描目录", "done": 0, "total": 0, "current": ""})
    files = collect_files(roots)
    if max_files and len(files) > max_files:
        files = files[:max_files]
    if not files:
        return {"error": "没有找到音频文件", "groups": [], "stats": {}}
    emit({"phase": f"发现 {len(files)} 个音频文件", "done": 0, "total": len(files), "current": ""})

    # ---------- 1. 元数据 ----------
    metas: list[dict] = []
    errors: list[dict] = []
    for i, f in enumerate(files, 1):
        if job.get("cancel"):
            break
        try:
            st = os.stat(f)
            cached = cache.get(f, st.st_size, st.st_mtime) if cache else None
            if cached and cached.get("probe"):
                metas.append(cached["probe"])
            else:
                m = pb.probe(f)
                metas.append(m)
                if cache:
                    cache.put(f, st.st_size, st.st_mtime, {"probe": m})
        except Exception as e:  # noqa: BLE001
            errors.append({"path": f, "stage": "probe", "error": str(e)[:200]})
        if i % 20 == 0 or i == len(files):
            emit({"phase": "读取元数据", "done": i, "total": len(files),
                  "current": Path(f).name})
    if job.get("cancel"):
        return {"error": "已取消", "groups": [], "stats": {}}

    # ---------- 2. 分组 ----------
    emit({"phase": "同曲归并", "done": 0, "total": 0, "current": ""})
    groups = gp.build_groups(metas)

    need: list[dict] = []
    for g in groups:
        if only_multi_version and len(g["versions"]) < 2:
            continue
        need.extend(g["versions"])
    # 同一文件可能落在多个组里，去重
    uniq: dict[str, dict] = {}
    for m in need:
        uniq[m["path"]] = m
    need = list(uniq.values())
    emit({"phase": f"待深度分析 {len(need)} 个文件（{len([g for g in groups if len(g['versions'])>1])} 首歌有多版）",
          "done": 0, "total": len(need), "current": ""})

    # ---------- 3. 深度分析 ----------
    workers = workers or max(1, min(6, (os.cpu_count() or 4) // 2))
    done = 0
    total = len(need)

    def work(m: dict) -> tuple[dict, dict | None]:
        p = m["path"]
        try:
            st = os.stat(p)
            if cache:
                c = cache.get(p, st.st_size, st.st_mtime)
                if c and c.get("analysis"):
                    return m, c["analysis"]
            a = an.analyze_audio(p, m)
            if cache:
                merged = cache.get(p, st.st_size, st.st_mtime) or {}
                merged.update({"probe": m, "analysis": a})
                cache.put(p, st.st_size, st.st_mtime, merged)
            return m, a
        except Exception as e:  # noqa: BLE001
            errors.append({"path": p, "stage": "analyze", "error": str(e)[:200]})
            return m, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, m): m for m in need}
        for fut in as_completed(futs):
            if job.get("cancel"):
                for f in futs:
                    f.cancel()
                break
            m, a = fut.result()
            done += 1
            if a:
                m.update(a)
            emit({"phase": "声学分析", "done": done, "total": total,
                  "current": m.get("name", "")})

    if job.get("cancel"):
        return {"error": "已取消", "groups": [], "stats": {}}

    # ---------- 4. 排行 ----------
    emit({"phase": "综合评分", "done": 0, "total": 0, "current": ""})
    out_groups = []
    for g in groups:
        if only_multi_version and len(g["versions"]) < 2:
            continue
        for v in g["versions"]:
            v.pop("_qhash", None)
        out_groups.append(gp.rank_group(g))

    multi = [g for g in out_groups if len(g["versions"]) > 1]
    gem = [g for g in multi
           if any((v.get("flags") and any(f["level"] == "bad" for f in v["flags"])) for v in g["versions"])]

    stats = {
        "files_found": len(files),
        "files_probed": len(metas),
        "files_analyzed": total,
        "songs_total": len(groups),
        "songs_multi": len(multi),
        "songs_with_problems": len(gem),
        "errors": len(errors),
        "elapsed_sec": round(time.time() - t0, 1),
        "roots": roots,
        "workers": workers,
    }
    return {"groups": out_groups, "stats": stats, "errors": errors[:200],
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
