"""播放顺序修正（OrderFix）。

问题
----
《同一首歌》文件夹里放着「歌名+歌星.wav」的一堆同曲异人版本。播放器（车载 U 盘、
便携机、部分 DLNA 端）读不出顺序，只能按文件"进文件夹的先后"播——因为：

1. FAT32/exFAT 的目录就是一个按写入顺序追加的链表，播放器按目录条目顺序读；
2. WAV 几乎没有可用的标签，播放器没有 track number 可排；
3. 文件名里没有序号时，很多播放器只做"文件名排序"——中文按字节比，
   排序结果既不是拼音也不是笔画。

一次做四件事，覆盖四种播放器实现
--------------------------------
| 手段 | 治哪种播放器 | 代价 |
|---|---|---|
| 文件名加序号 `01 歌名 - 歌星.wav` | 按文件名/自然序排序的（绝大多数） | 改名 |
| 生成 m3u8(BOM) + m3u(GBK) 播放列表 | 支持列表的（车载、foobar、JRiver） | 多两个文件 |
| 写 ID3v2 + RIFF INFO 标签 | 按标签排序的（JRiver、MusicBee） | 改文件内容 |
| 按目标顺序设文件时间戳 | 按"日期"排序的（部分车载） | 改时间 |
| 按目标顺序复制成新目录 | 按目录条目顺序读的 FAT/exFAT 设备 | 占一份空间 |

默认只出方案（dry run），要显式 apply 才动文件；每次 apply 落一份撤销清单。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

AUDIO_EXT = {".wav", ".flac", ".ape", ".wv", ".aif", ".aiff", ".mp3", ".m4a",
             ".dsf", ".dff", ".ogg", ".opus", ".wma", ".alac"}

# 歌名/歌星分隔符，从长到短匹配；只取最右一个，避免歌名里的 "-" 被误切
SEPARATORS = [" - ", " – ", " — ", " － ", " + ", "、", "·", " -", "- ", "_", "-", "+", "—"]

TRACK_NO_RE = re.compile(r"^\s*(?:cd\s*\d+\s*[-_.]?\s*)?(\d{1,3})\s*[-_.\s\)]\s*", re.I)
VERSION_HINT_RE = re.compile(
    r"^(live|remix|rmx|acoustic|instrumental|inst|karaoke|demo|edit|mono|stereo|"
    r"现场|伴奏|纯音乐|混音|重制|复刻|片段|版)$", re.I)
ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# --------------------------------------------------------------------------- #
# 文本处理：拼音 / 自然序
# --------------------------------------------------------------------------- #
_PY = None
_PY_LOADED = False


def _pinyin(text: str) -> str | None:
    """汉字转拼音（无声调，逐字）。没装 pypinyin 返回 None，由调用方降级。"""
    global _PY, _PY_LOADED
    if not _PY_LOADED:
        _PY_LOADED = True
        try:
            from pypinyin import lazy_pinyin  # noqa: PLC0415
            _PY = lazy_pinyin
        except Exception:  # noqa: BLE001
            _PY = None
    if _PY is None:
        return None
    try:
        return "".join(_PY(text, errors="default")).lower()
    except Exception:  # noqa: BLE001
        return None


def natural_key(text: str) -> tuple:
    """数字按数值比较："第2首" 排在 "第10首" 前面。"""
    parts = re.split(r"(\d+)", text or "")
    out = []
    for p in parts:
        if not p:
            continue
        out.append((1, int(p), "") if p.isdigit() else (0, 0, p.lower()))
    return tuple(out)


def sort_key(text: str) -> tuple:
    """中文优先按拼音，装不上拼音库则退化为码位序（会在界面里标注）。"""
    py = _pinyin(text)
    return natural_key(py if py is not None else (text or ""))


def pinyin_available() -> bool:
    return _pinyin("测试") is not None


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip()


def split_title_artist(stem: str) -> tuple[str, str, int | None]:
    """按「歌名 + 歌星」切分。返回 (歌名, 歌星, 原曲序号)。"""
    s = _norm(stem)
    track = None
    m = TRACK_NO_RE.match(s)
    if m:
        track = int(m.group(1))
        s = s[m.end():].strip()

    for sep in SEPARATORS:
        idx = s.rfind(sep)
        if idx <= 0:
            continue
        left, right = s[:idx].strip(), s[idx + len(sep):].strip()
        if not left or not right or len(right) > 40:
            continue
        if VERSION_HINT_RE.match(right):      # "歌名 - Live" 属于版本标注，不是歌星
            continue
        return left, right, track
    return s, "", track


def _clean_name(s: str) -> str:
    s = ILLEGAL_RE.sub("_", _norm(s))
    return re.sub(r"\s{2,}", " ", s).strip(" .")


# --------------------------------------------------------------------------- #
# 读目录
# --------------------------------------------------------------------------- #
def _duration(path: Path) -> float | None:
    try:
        import mutagen  # noqa: PLC0415
        f = mutagen.File(str(path))
        if f is not None and f.info is not None:
            return round(float(f.info.length), 2)
    except Exception:  # noqa: BLE001
        pass
    return None


def _tag_artist(path: Path) -> tuple[str | None, str | None]:
    """读标签里的 title/artist，作为文件名解析失败时的兜底。"""
    try:
        import mutagen  # noqa: PLC0415
        f = mutagen.File(str(path), easy=True)
        if f is None:
            return None, None
        t = (f.get("title") or [None])[0]
        a = (f.get("artist") or [None])[0]
        return (str(t) if t else None), (str(a) if a else None)
    except Exception:  # noqa: BLE001
        return None, None


def volume_fs(path: Path) -> str | None:
    """取盘符的文件系统类型（NTFS / exFAT / FAT32）。决定要不要重建目录顺序。"""
    if os.name != "nt":
        return None
    try:
        import ctypes  # noqa: PLC0415
        drive = os.path.splitdrive(str(path.resolve()))[0]
        if not drive:
            return None
        root = drive + "\\"
        name = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), name, 261, None, None, None, fs, 261)
        return fs.value if ok else None
    except Exception:  # noqa: BLE001
        return None


def scan(folder: str | Path) -> dict:
    folder = Path(folder)
    if not folder.is_dir():
        return {"error": f"Not a folder: {folder}"}
    files = []
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
            continue
        if p.name.startswith((".", "~$")):
            continue
        title, artist, track = split_title_artist(p.stem)
        t_tag, a_tag = (None, None)
        if not artist:
            t_tag, a_tag = _tag_artist(p)
            title = title or (t_tag or p.stem)
            artist = a_tag or ""
        st = p.stat()
        files.append({
            "path": str(p),
            "name": p.name,
            "ext": p.suffix.lower(),
            "title": title,
            "artist": artist,
            "track_hint": track,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "ctime": st.st_ctime,
            "duration": _duration(p),
        })
    return {"folder": str(folder), "fs": volume_fs(folder), "files": files,
            "pinyin": pinyin_available()}


# --------------------------------------------------------------------------- #
# 出方案（只读）
# --------------------------------------------------------------------------- #
SORT_MODES = {
    "title": "Title → Artist",
    "artist": "Artist → Title",
    "name": "Current filename (natural order)",
    "mtime": "File time",
}


def _order(files: list[dict], sort_by: str) -> list[dict]:
    if sort_by == "artist":
        return sorted(files, key=lambda f: (sort_key(f["artist"] or "~"),
                                            sort_key(f["title"]), f["name"]))
    if sort_by == "name":
        return sorted(files, key=lambda f: sort_key(Path(f["name"]).stem))
    if sort_by == "mtime":
        return sorted(files, key=lambda f: f["mtime"])
    return sorted(files, key=lambda f: (sort_key(f["title"]),
                                        sort_key(f["artist"] or "~"), f["name"]))


def build_plan(folder: str | Path, sort_by: str = "title",
               prefix: str = "num", sep: str = " - ",
               recursive: bool = False) -> dict:
    """生成目标顺序与目标文件名。不碰任何文件。"""
    info = scan(folder)
    if info.get("error"):
        return info
    files = info["files"]
    if recursive:
        for sub in Path(folder).rglob("*"):
            if sub.is_dir() and sub != Path(folder):
                for p in sorted(sub.iterdir()):
                    if p.is_file() and p.suffix.lower() in AUDIO_EXT:
                        t, a, tr = split_title_artist(p.stem)
                        st = p.stat()
                        files.append({"path": str(p), "name": p.name,
                                      "ext": p.suffix.lower(), "title": t, "artist": a,
                                      "track_hint": tr, "size": st.st_size,
                                      "mtime": st.st_mtime, "ctime": st.st_ctime,
                                      "duration": _duration(p)})
    if not files:
        return {**info, "error": "No audio files in this folder"}

    ordered = _order(files, sort_by)
    width = max(2, len(str(len(ordered))))
    taken: set[str] = set()
    olds = {f["name"] for f in ordered}

    for i, f in enumerate(ordered, 1):
        stem = _clean_name(f"{f['title']}{sep}{f['artist']}" if f["artist"] else f["title"])
        if prefix == "num":
            base = f"{i:0{width}d} {stem}"
        elif prefix == "numdot":
            base = f"{i:0{width}d}.{stem}"
        elif prefix == "numdash":
            base = f"{i:0{width}d}-{stem}"
        else:
            base = stem
        name = base + f["ext"]
        k = 2
        while name.lower() in taken:
            name = f"{base} ({k}){f['ext']}"
            k += 1
        taken.add(name.lower())
        f["index"] = i
        f["new_name"] = name
        f["renamed"] = name != f["name"]
        f["collision"] = name != f["name"] and name in olds

    return {
        "folder": info["folder"],
        "fs": info.get("fs"),
        "pinyin": info.get("pinyin"),
        "sort_by": sort_by,
        "sort_label": SORT_MODES.get(sort_by, sort_by),
        "prefix": prefix,
        "files": ordered,
        "stats": {
            "count": len(ordered),
            "to_rename": sum(1 for f in ordered if f["renamed"]),
            "collisions": sum(1 for f in ordered if f.get("collision")),
            "artists": len({f["artist"] for f in ordered if f["artist"]}),
            "titles": len({f["title"] for f in ordered}),
            "total_bytes": sum(f["size"] for f in ordered),
        },
    }


# --------------------------------------------------------------------------- #
# 标签：RIFF INFO（Windows 认）+ ID3v2（JRiver/foobar 认）
# --------------------------------------------------------------------------- #
_INFO_TAGS = {"title": b"INAM", "artist": b"IART", "album": b"IPRD",
              "track": b"ITRK", "comment": b"ICMT", "software": b"ISFT"}


def _write_riff_info(path: Path, fields: dict[str, str]) -> bool:
    """写/替换 WAV 的 LIST/INFO 块。流式重写到临时文件再原子替换，内存恒定。"""
    esc = {k: (v.encode("utf-8") + b"\x00") for k, v in fields.items() if k in _INFO_TAGS}
    if not esc:
        return False
    body = b"INFO"
    for k, v in esc.items():
        pad = b"\x00" if len(v) % 2 else b""
        body += _INFO_TAGS[k] + len(v).to_bytes(4, "little") + v + pad
    if len(body) % 2:
        body += b"\x00"
    new_chunk = b"LIST" + len(body).to_bytes(4, "little") + body

    tmp = path.with_suffix(path.suffix + ".dsc-tmp")
    try:
        with path.open("rb") as src, tmp.open("wb") as dst:
            head = src.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return False
            dst.write(head)
            total = 12
            while True:
                ch = src.read(8)
                if len(ch) < 8:
                    break
                cid, csize = ch[:4], int.from_bytes(ch[4:8], "little")
                payload = src.read(csize)
                if len(payload) < csize:
                    break
                pad = b"\x00" if csize % 2 else b""
                if csize % 2:
                    src.read(1)
                if cid == b"LIST" and payload[:4] == b"INFO":
                    continue                      # 丢掉旧的 INFO 块
                dst.write(ch)
                dst.write(payload)
                if pad:
                    dst.write(pad)
                total += 8 + csize + (csize % 2)
            dst.write(new_chunk)
            total += len(new_chunk)
            dst.seek(4)
            dst.write((total - 8).to_bytes(4, "little"))
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def write_tags(path: Path, title: str, artist: str, index: int) -> dict:
    """写标签。WAV 双写（INFO + ID3），其它格式走 mutagen easy 接口。"""
    res = {"info": False, "id3": False}
    fields = {"title": title, "artist": artist, "track": f"{index}",
              "album": path.parent.name, "software": "DupeScope"}
    if path.suffix.lower() == ".wav":
        res["info"] = _write_riff_info(path, fields)
        try:
            from mutagen.id3 import TALB, TENC, TIT2, TPE1, TRCK  # noqa: PLC0415
            from mutagen.wave import WAVE  # noqa: PLC0415

            w = WAVE(str(path))
            if w.tags is None:
                w.add_tags()
            w.tags.add(TIT2(encoding=3, text=title))
            w.tags.add(TPE1(encoding=3, text=artist))
            w.tags.add(TRCK(encoding=3, text=str(index)))
            w.tags.add(TALB(encoding=3, text=path.parent.name))
            w.tags.add(TENC(encoding=3, text="DupeScope"))
            w.save()
            res["id3"] = True
        except Exception as e:  # noqa: BLE001
            res["id3_error"] = str(e)[:120]
    else:
        try:
            import mutagen  # noqa: PLC0415

            f = mutagen.File(str(path), easy=True)
            if f is not None:
                if f.tags is None:
                    f.add_tags()
                f["title"] = title
                f["artist"] = artist
                f["tracknumber"] = str(index)
                f["album"] = path.parent.name
                f.save()
                res["id3"] = True
        except Exception as e:  # noqa: BLE001
            res["id3_error"] = str(e)[:120]
    return res


# --------------------------------------------------------------------------- #
# 时间戳：给「按日期排序」的播放器
# --------------------------------------------------------------------------- #
def _set_file_time(path: Path, ts: float) -> bool:
    if os.name != "nt":
        try:
            os.utime(path, (ts, ts))
            return True
        except OSError:
            return False
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p]
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        h = k32.CreateFileW(str(path), GENERIC_WRITE, 3, None, OPEN_EXISTING, 0, None)
        if h in (0, wintypes.HANDLE(-1).value):
            return False
        ft = int((ts + 11644473600) * 10_000_000)
        buf = ctypes.c_longlong(ft)
        ok = k32.SetFileTime(h, ctypes.byref(buf), ctypes.byref(buf), ctypes.byref(buf))
        k32.CloseHandle(h)
        return bool(ok)
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 播放列表
# --------------------------------------------------------------------------- #
def write_playlists(folder: Path, items: list[dict], base_name: str | None = None) -> list[str]:
    """写两份：m3u8(UTF-8 BOM，现代播放器) 与 m3u(GBK，老车载)。"""
    folder = Path(folder)
    base = base_name or folder.name or "playlist"
    lines = ["#EXTM3U"]
    for f in items:
        dur = int(f.get("duration") or -1)
        title = f"{f['title']} - {f['artist']}" if f["artist"] else f["title"]
        lines.append(f"#EXTINF:{dur},{title}")
        lines.append(f["name"])
    text = "\r\n".join(lines) + "\r\n"

    out = []
    p8 = folder / f"{base}.m3u8"
    p8.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    out.append(str(p8))
    pg = folder / f"{base}.m3u"
    try:
        pg.write_bytes(text.encode("gbk", errors="replace"))
        out.append(str(pg))
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------- #
# 应用 / 撤销
# --------------------------------------------------------------------------- #
DEFAULT_ACTIONS = {"rename": True, "playlist": True, "tags": False,
                   "timestamps": False, "fatorder": False}


def _manifest_dir(folder: Path) -> Path:
    d = folder / ".dupescope"
    d.mkdir(exist_ok=True)
    return d


def apply_plan(folder: str | Path, sort_by: str = "title", prefix: str = "num",
               actions: dict | None = None, confirm: bool = False,
               reverse_time: bool = False) -> dict:
    """执行。confirm=False 时只回方案，绝不改文件。"""
    folder = Path(folder)
    plan = build_plan(folder, sort_by=sort_by, prefix=prefix)
    if plan.get("error"):
        return plan
    acts = {**DEFAULT_ACTIONS, **(actions or {})}

    if not confirm:
        return {"dry_run": True, **plan, "actions": acts}

    items = plan["files"]
    manifest = {
        "version": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "folder": str(folder),
        "sort_by": sort_by,
        "actions": acts,
        "renames": [],
        "times": [],
        "playlists": [],
        "copydir": None,
        "errors": [],
    }

    # ---- 1. 改名（两阶段，避免 A→B 同时 B→A 的撞车）----
    if acts.get("rename"):
        phase = []
        for i, f in enumerate(items):
            if not f["renamed"]:
                continue
            src = Path(f["path"])
            if f.get("collision"):
                manifest["errors"].append({"path": str(src), "error": "Target filename already exists; skipped"})
                f["new_name"] = f["name"]
                continue
            tmp = src.with_name(f".dsc-tmp-{int(time.time())}-{i}{src.suffix}")
            try:
                os.rename(src, tmp)
                phase.append((tmp, src.parent / f["new_name"], src.name))
            except OSError as e:
                manifest["errors"].append({"path": str(src), "error": f"Rename failed: {e}"})
        for tmp, dst, old_name in phase:
            try:
                os.rename(tmp, dst)
                manifest["renames"].append([str(dst.parent / old_name), str(dst)])
                f2 = next((x for x in items if x["name"] == old_name), None)
                if f2:
                    f2["path"] = str(dst)
                    f2["name"] = dst.name
            except OSError as e:
                manifest["errors"].append({"path": str(dst), "error": f"Write failed: {e}"})
                try:
                    os.rename(tmp, tmp.parent / old_name)
                except OSError:
                    pass

    # ---- 2. 标签 ----
    if acts.get("tags"):
        for f in items:
            try:
                r = write_tags(Path(f["path"]), f["title"], f["artist"], f["index"])
                f["tags"] = r
            except Exception as e:  # noqa: BLE001
                manifest["errors"].append({"path": f["path"], "error": f"Tag write failed: {e}"})
            plan["tag_support"] = {"riff_info": True,
                                   "note": "WAV: writes both INFO (Windows) and ID3 (JRiver/foobar)"}

    # ---- 3. 时间戳 ----
    if acts.get("timestamps"):
        now = time.time()
        n = len(items)
        for i, f in enumerate(items):
            p = Path(f["path"])
            try:
                st = p.stat()
                manifest["times"].append({"path": str(p), "mtime": st.st_mtime,
                                          "ctime": st.st_ctime, "atime": st.st_atime})
                off = (i if reverse_time else (n - 1 - i)) * 60
                _set_file_time(p, now - off)
            except OSError as e:
                manifest["errors"].append({"path": str(p), "error": f"Timestamp change failed: {e}"})

    # ---- 4. 播放列表（放最后，文件名已定）----
    if acts.get("playlist"):
        try:
            manifest["playlists"] = write_playlists(folder, items)
        except Exception as e:  # noqa: BLE001
            manifest["errors"].append({"path": str(folder), "error": f"Playlist write failed: {e}"})

    # ---- 5. 重建目录顺序（复制，不动原件）----
    if acts.get("fatorder"):
        dest = folder / "_ordered-copy"
        try:
            dest.mkdir(exist_ok=True)
            for f in items:
                shutil.copy2(f["path"], dest / Path(f["path"]).name)
            manifest["copydir"] = str(dest)
            write_playlists(dest, items, base_name=folder.name or "playlist")
        except Exception as e:  # noqa: BLE001
            manifest["errors"].append({"path": str(dest), "error": f"Copy failed: {e}"})

    mpath = _manifest_dir(folder) / f"undo-{time.strftime('%Y%m%d-%H%M%S')}.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"dry_run": False, "manifest": str(mpath), "plan": plan,
            "actions": acts, "stats": plan["stats"],
            "errors": manifest["errors"], "playlists": manifest["playlists"],
            "copydir": manifest["copydir"], "renames": len(manifest["renames"])}


def list_manifests(folder: str | Path) -> list[dict]:
    d = Path(folder) / ".dupescope"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("undo-*.json"), reverse=True):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            out.append({"manifest": str(p), "created": m.get("created"),
                        "renames": len(m.get("renames") or []),
                        "actions": m.get("actions")})
        except Exception:  # noqa: BLE001
            continue
    return out[:20]


def undo(manifest_path: str | Path) -> dict:
    """按撤销清单把改名倒回去、时间恢复、删掉自己生成的播放列表。"""
    p = Path(manifest_path)
    if not p.is_file():
        return {"error": "Undo manifest not found"}
    m = json.loads(p.read_text(encoding="utf-8"))
    done, errors = 0, []

    for old, new in reversed(m.get("renames") or []):
        try:
            if Path(new).exists() and not Path(old).exists():
                os.rename(new, old)
                done += 1
        except OSError as e:
            errors.append({"path": new, "error": str(e)})

    for t in m.get("times") or []:
        try:
            if Path(t["path"]).exists():
                _set_file_time(Path(t["path"]), t["mtime"])
        except Exception:  # noqa: BLE001
            pass

    for pl in m.get("playlists") or []:
        try:
            Path(pl).unlink(missing_ok=True)
        except OSError:
            pass

    return {"restored": done, "errors": errors,
            "note": "The copied _ordered-copy folder is not removed automatically; confirm manually before deleting."}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="DupeScope · 播放顺序修正")
    ap.add_argument("dir", nargs="?", help="要整理的文件夹（如《同一首歌》）")
    ap.add_argument("--sort", default="title", choices=list(SORT_MODES))
    ap.add_argument("--prefix", default="num", choices=["num", "numdot", "numdash", "none"])
    ap.add_argument("--apply", action="store_true", help="真的改文件（默认只打印方案）")
    ap.add_argument("--tags", action="store_true", help="同时写标签")
    ap.add_argument("--times", action="store_true", help="同时改时间戳")
    ap.add_argument("--fatorder", action="store_true", help="同时复制一份有序副本")
    ap.add_argument("--no-rename", action="store_true", help="不改名，只做其它")
    ap.add_argument("--undo", metavar="MANIFEST", help="撤销某次操作")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.undo:
        r = undo(a.undo)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if not r.get("error") else 1

    if not a.dir:
        ap.error("请给出要整理的文件夹；或 --undo 指定撤销清单")

    acts = {"rename": not a.no_rename, "playlist": True,
            "tags": a.tags, "timestamps": a.times, "fatorder": a.fatorder}
    r = apply_plan(a.dir, sort_by=a.sort, prefix=a.prefix, actions=acts, confirm=a.apply)

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if not r.get("error") else 1
    if r.get("error"):
        print("×", r["error"])
        return 1
    plan = r.get("plan") or r
    print(f"目录: {plan['folder']}   文件系统: {plan.get('fs') or '?'}   "
          f"拼音排序: {'是' if plan.get('pinyin') else '否(退化码位序)'}")
    print(f"排序依据: {plan['sort_label']}   共 {plan['stats']['count']} 个文件\n")
    for f in plan["files"]:
        mark = "→" if f["renamed"] else " "
        print(f"  {f['index']:02d} {mark} {f['name']:<38}  ⇒  {f['new_name']}")
    if not a.apply:
        print("\n（只出方案，没有改动任何文件。加 --apply 才执行）")
        return 0
    print(f"\n已执行。改名 {r.get('renames', 0)} 个。")
    if r.get("playlists"):
        print("播放列表: " + "; ".join(Path(x).name for x in r["playlists"]))
    if r.get("errors"):
        print("有问题的文件:")
        for e in r["errors"][:10]:
            print("  -", e.get("path"), e.get("error"))
    print(f"撤销清单: {r.get('manifest')}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
