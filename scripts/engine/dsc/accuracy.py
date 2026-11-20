"""位对齐校验（AccurateRip v1/v2 + CTDB 的可选本地实现）。

为什么自己实现而不只调 CUETools：
  CUETools.exe /verify 能查 AccurateRip，但官方注明"完成后不自动关闭、无法循环调用"，
  所以它只适合 GUI 复核，做不了 100 GB 库的静默批处理。本模块把可自动的部分
  （cue TOC -> discID -> checksum -> 查库 -> 比对）用纯 Python 实现，可并发、可缓存。

硬边界（必须诚实）：
  AccurateRip 用整盘 TOC 算 discID。单曲 WAV/FLAC 没有 TOC、也没有原始抓轨偏移信息，
  物理上无法校验——这类文件直接返回 need_cue，不做任何"看起来像能验"的假动作。
  校验要求 16-bit 立体声 PCM，且已由抓轨软件做过驱动器偏移校正。

口径来源：
  v1/v2 checksum：AccurateRip 官方算法（skip 帧 2939/2940，1 基位置加权；
                  v2 把 64 位乘积高 32 位折回）。见 dart_accuraterip / cyanrip。
  discID 与 DB 格式：cyanrip src/accurip.c（GPL）。
"""
from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

from . import ffmpeg_bin

SKIP_FRAMES = 2940          # 首轨跳 2939，末轨跳 2940（见 _checksum）
AR_HOST = "http://www.accuraterip.com"
UA = "DupeScope/1.2 (local, non-commercial AccurateRip query)"

# --------------------------------------------------------------------------- #
# cue 解析
# --------------------------------------------------------------------------- #
_REM = re.compile(r'^\s*REM\s+([\w-]+)\s+(.+?)\s*$', re.I)
_FILE = re.compile(r'^\s*FILE\s+"(.+?)"\s+(\w+)\s*$', re.I)
_TRACK = re.compile(r'^\s*TRACK\s+(\d{1,2})\s+(\w+)\s*$', re.I)
_INDEX = re.compile(r'^\s*INDEX\s+(\d{1,2})\s+(\d+):(\d+):(\d+)\s*$', re.I)


@dataclass
class CueTrack:
    number: int
    start_msf: str          # mm:ss:ff
    start_frame: int        # 绝对 CD 帧（含 150 前导）
    file: str


@dataclass
class CueSheet:
    path: str
    files: list[str]
    tracks: list[CueTrack]
    leadout_frame: int | None   # 若 cue 里无 lead-out，需由音频时长推算
    cddb_id: str | None
    title: str | None


def _msf_to_frame(m: int, s: int, f: int) -> int:
    return (m * 60 + s) * 75 + f


def parse_cue(path: str | Path) -> CueSheet:
    """解析标准 cue；只取算 discID 所需的最小集合，容错坏行。"""
    path = str(path)
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    files: list[str] = []
    tracks: list[CueTrack] = []
    cddb: str | None = None
    title: str | None = None
    leadout = None
    cur_file = ""
    cur: dict | None = None

    for line in raw.splitlines():
        m = _FILE.match(line)
        if m:
            cur_file = m.group(1)
            if cur_file not in files:
                files.append(cur_file)
            continue
        m = _TRACK.match(line)
        if m:
            if cur and cur.get("start") is not None:
                tracks.append(CueTrack(cur["no"], cur["msf"], cur["start"], cur["file"]))
            cur = {"no": int(m.group(1)), "msf": "", "start": None, "file": cur_file}
            continue
        m = _INDEX.match(line)
        if m and cur and m.group(1) == "01" and cur.get("start") is None:
            mm, ss, ff = int(m.group(2)), int(m.group(3)), int(m.group(4))
            cur["msf"] = f"{mm:02d}:{ss:02d}:{ff:02d}"
            cur["start"] = _msf_to_frame(mm, ss, ff)
            continue
        m = _REM.match(line)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == "cddb":
                cddb = val
            elif key == "title" and title is None:
                title = val.strip('"')
            # 部分 cue 把 lead-out 写在 REM 里（如 REM LEAD-OUT 74:12:00）
            elif "lead" in key and re.match(r'\d+:\d+:\d+', val):
                mm, ss, ff = map(int, val.split(":")[:3])
                leadout = _msf_to_frame(mm, ss, ff)

    if cur and cur.get("start") is not None:
        tracks.append(CueTrack(cur["no"], cur["msf"], cur["start"], cur["file"]))

    # 按 track 号排序，去掉重复
    tracks = sorted({t.number: t for t in tracks}.values(), key=lambda t: t.number)
    # cue 无 lead-out 时留空，由 verify_cue 从音频时长推算

    return CueSheet(path, files, tracks, leadout, cddb, title)


def disc_ids(tracks: list[CueTrack], leadout_frame: int) -> tuple[int, int]:
    """AccurateRip discID（cyanrip get_accurip_ids 口径）。

    offset 直接用 INDEX 01 的绝对 LSN（cue 写 00:00:00 就是 0；若 cue 把
    INDEX 01 写成 00:02:00，那 150 是真实存在的 pregap，不能人为再减）。
      id1 = (Σ track_frame + leadout_frame) & 0xFFFFFFFF
      id2 = (Σ track_frame*(i+1) + leadout_frame*(n+1)) & 0xFFFFFFFF
    """
    frames = [t.start_frame for t in tracks]
    n = len(frames)
    id1 = (sum(frames) + leadout_frame) & 0xFFFFFFFF
    id2 = 0
    for i, fr in enumerate(frames, 1):
        id2 = (id2 + fr * i) & 0xFFFFFFFF
    id2 = (id2 + leadout_frame * (n + 1)) & 0xFFFFFFFF
    return id1, id2


def cddb_id(tracks: list[CueTrack], leadout_frame: int) -> int:
    """标准 CDDB disc id（用作 URL 末段；查库主要靠 id1/id2）。"""
    def acc(n: int) -> int:
        while n:
            n = (n >> 1) ^ (n & 1 and 0xEDB88320 or 0)
        return n & 0xFFFFFFFF
    total = 0
    for t in tracks:
        _, s, f = t.start_msf.split(":")
        total += acc(int(s) * 75 + int(f))
    mm, ss, ff = leadout_frame // 4500, (leadout_frame // 75) % 60, leadout_frame % 75
    dur = (mm * 60 + ss) - 2
    return (((total & 0xFFFFFFFF) % 255) << 24 | dur << 8 | len(tracks)) & 0xFFFFFFFF


# --------------------------------------------------------------------------- #
# checksum：直接对解码出的 16-bit stereo little-endian PCM 计算
# --------------------------------------------------------------------------- #
def checksum_from_pcm(pcm: bytes, is_first: bool, is_last: bool) -> tuple[int, int]:
    """返回 (v1, v2)。pcm 长度须为 4 的倍数（一个 stereo frame = 4 字节）。"""
    n = len(pcm) // 4
    if n == 0:
        return 0, 0
    start = SKIP_FRAMES - 1 if is_first else 0
    end = max(start, n - SKIP_FRAMES) if is_last else n
    if end > n:
        end = n
    v1 = 0
    v2 = 0
    # dart 参考实现把每 4 字节（一帧 stereo s16le）当作一个 uint32
    for i in range(start, end):
        sample = struct.unpack_from("<I", pcm, i * 4)[0]   # little-endian uint32
        mult = sample * (i + 1)            # i 是 0 基，位置权重是 1 基，跳过的帧照样推进权重
        v1 = (v1 + (mult & 0xFFFFFFFF)) & 0xFFFFFFFF
        v2 = (v2 + (mult & 0xFFFFFFFF) + ((mult >> 32) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return v1, v2


def decode_track_pcm(path: str | Path, start_sec: float = 0.0,
                     end_sec: float | None = None) -> bytes:
    """用随包 ffmpeg 把音轨解成 s16le stereo。可裁剪 [start_sec, end_sec)。

    用 output-side -ss/-t（放在 -i 之后）做精确裁剪：会完整解码后丢弃，
    保证 checksum 不受关键帧对齐影响。16-bit 立体声 / 44100 Hz 是 CD 原始规格。
    """
    cmd = [ffmpeg_bin.ffmpeg(), "-v", "error", "-i", str(path)]
    if start_sec and start_sec > 0:
        cmd += ["-ss", f"{start_sec:.6f}"]
    if end_sec is not None and end_sec > start_sec:
        cmd += ["-t", f"{end_sec - start_sec:.6f}"]
    cmd += ["-f", "s16le", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=1800,
                       creationflags=0x08000000)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 解码失败: {(r.stderr or b'').decode('utf-8', 'replace')[:200]}")
    return r.stdout


# --------------------------------------------------------------------------- #
# 查库 + 解析 .bin
# --------------------------------------------------------------------------- #
def build_url(ntracks: int, id1: int, id2: int, cddb: int) -> str:
    h = f"{id1:08x}"
    c1, c2, c3 = h[7], h[6], h[5]     # id1 末三位十六进制，反序
    return (f"{AR_HOST}/accuraterip/{c1}/{c2}/{c3}/"
            f"dBAR-{ntracks:03d}-{id1:08x}-{id2:08x}-{cddb:08x}.bin")


@dataclass
class ARChecksums:
    confidence: int
    v1: int
    v2: int


def parse_bin(data: bytes, ntracks: int, id1: int, id2: int, cddb: int) -> list[list[ARChecksums]]:
    """返回 entries，每个 entry 是一台机器提交的全盘 checksum 列表。"""
    entries: list[list[ARChecksums]] = []
    pos = 0
    stride = 13 + ntracks * 9
    while pos + stride <= len(data):
        nt = data[pos]
        e_id1, e_id2, e_cddb = struct.unpack_from("<III", data, pos + 1)
        if nt != ntracks or e_id1 != id1 or e_id2 != id2:
            pos += 1          # 容错：不按 stride 硬跳，逐字节找下一个合法头
            continue
        entry: list[ARChecksums] = []
        off = pos + 13
        for _ in range(nt):
            conf, v1, v2 = struct.unpack_from("<BII", data, off)
            entry.append(ARChecksums(conf, v1, v2))
            off += 9
        entries.append(entry)
        pos += stride
    return entries


def fetch_checksums(url: str, timeout: float = 12) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def verify_cue(cue_path: str | Path, leadout_frame: int | None = None,
               fetch: bool = True, fetcher=None) -> dict:
    """对一张整盘 cue 做位对齐。只读，不改任何音频。

    返回 dict：status = ok | mismatch | not_found | need_cue | offline | error
    """
    cue = parse_cue(cue_path)
    if len(cue.tracks) < 1:
        return {"status": "need_cue", "reason": "cue 里没有 TRACK",
                "cue": cue.path}
    if not cue.files:
        return {"status": "need_cue", "reason": "cue 里没有 FILE",
                "cue": cue.path}

    # lead-out：优先 cue 里的；否则用最后一个音频文件的时长推算
    lo = cue.leadout_frame or leadout_frame
    if lo is None:
        from . import probe as pb
        last = Path(cue.path).parent / cue.files[0]
        meta = pb.probe(last)
        dur = meta.get("duration") or 0
        lo = cue.tracks[-1].start_frame + int(dur * 75)
        if lo <= cue.tracks[-1].start_frame:
            return {"status": "need_cue",
                    "reason": "cue 无 lead-out 且无法从音频时长推算，请在 cue 里补 REM LEAD-OUT",
                    "cue": cue.path}

    id1, id2 = disc_ids(cue.tracks, lo)
    cddb = int(cue.cddb_id, 16) if cue.cddb_id and re.fullmatch(r'[0-9a-fA-F]{8}', cue.cddb_id) \
        else cddb_id(cue.tracks, lo)
    url = build_url(len(cue.tracks), id1, id2, cddb)

    out = {
        "cue": cue.path, "title": cue.title, "files": cue.files,
        "tracks": len(cue.tracks), "disc_id1": f"{id1:08x}", "disc_id2": f"{id2:08x}",
        "cddb": f"{cddb:08x}", "url": url, "status": "not_found",
        "results": [], "best_confidence": 0,
    }
    if not fetch:
        out["status"] = "offline"
        out["reason"] = "仅本地计算（fetch=false）"
        return out

    try:
        data = (fetcher(url) if fetcher else fetch_checksums(url))
    except Exception as e:  # noqa: BLE001
        out["status"] = "offline"
        out["reason"] = f"查询 AccurateRip 失败：{type(e).__name__}"
        return out
    if not data:
        out["status"] = "not_found"
        out["reason"] = "库里没有这张碟（可能是小众/自行刻录）"
        return out

    entries = parse_bin(data, len(cue.tracks), id1, id2, cddb)
    if not entries:
        out["reason"] = "库返回的数据无法解析"
        return out

    # 逐轨算本地 checksum，与库里所有提交比对，取最高 confidence
    # 处理两种 cue：一轨一文件（FILE 各自不同）/ 单文件整盘（所有轨 INDEX 段在同一文件）
    base = Path(cue.path).parent
    n = len(cue.tracks)
    for i, t in enumerate(cue.tracks):
        fp = base / t.file
        if not fp.is_file():
            out["results"].append({"track": t.number, "status": "missing_file",
                                   "file": t.file})
            continue
        # 区间换算。一轨一文件：各自文件从 0 解到结尾；单文件整盘：按 INDEX 切。
        first_frame = cue.tracks[0].start_frame
        if t.file == cue.tracks[0].file:
            # 单文件：本轨起点相对首轨，末轨到 leadout，其余到下一轨起点
            start_sec = (t.start_frame - first_frame) / 75.0
            if i == n - 1:
                end_sec = (lo - first_frame) / 75.0
            elif cue.tracks[i + 1].file == t.file:
                end_sec = (cue.tracks[i + 1].start_frame - first_frame) / 75.0
            else:
                end_sec = None
            if end_sec is not None and end_sec <= start_sec:
                end_sec = None
        else:
            start_sec, end_sec = 0.0, None
        try:
            pcm = decode_track_pcm(fp, start_sec, end_sec)
            v1, v2 = checksum_from_pcm(pcm, i == 0, i == n - 1)
        except Exception as e:  # noqa: BLE001
            out["results"].append({"track": t.number, "status": "decode_error",
                                   "error": str(e)[:120]})
            continue
        best = None
        for entry in entries:
            if i < len(entry) and entry[i].v1 == v1:
                if best is None or entry[i].confidence > best.confidence:
                    best = entry[i]
        if best:
            out["best_confidence"] = max(out["best_confidence"], best.confidence)
            out["results"].append({"track": t.number, "status": "verified",
                                   "confidence": best.confidence,
                                   "v1": f"{v1:08x}", "v2": f"{v2:08x}",
                                   "file": t.file})
        else:
            out["results"].append({"track": t.number, "status": "mismatch",
                                   "v1": f"{v1:08x}", "v2": f"{v2:08x}",
                                   "file": t.file})

    bad = [r for r in out["results"] if r["status"] in ("mismatch", "decode_error", "missing_file")]
    out["status"] = "ok" if not bad else "mismatch"
    out["submissions"] = len(entries)
    if bad:
        out["reason"] = f"{len(bad)} 轨与库不一致（可能是抓轨错误或经过重编码）"
    else:
        out["reason"] = f"全部 {n} 轨与库逐位一致，最高 confidence {out['best_confidence']}"
    return out


def find_cues(roots: list[str]) -> list[str]:
    """在给定根目录下找 cue；跳过隐藏目录。"""
    out: list[str] = []
    for r in roots:
        p = Path(r)
        if p.is_file() and p.suffix.lower() == ".cue":
            out.append(str(p))
            continue
        if not p.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "$", "System Volume"))]
            for fn in filenames:
                if fn.lower().endswith(".cue"):
                    out.append(str(Path(dirpath) / fn))
    return sorted(out)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", help="包含 cue 的专辑目录")
    ap.add_argument("--no-net", action="store_true", help="只本地算 discID，不联网查库")
    a = ap.parse_args()
    cues = find_cues([a.dir])
    if not cues:
        print(json.dumps({"status": "need_cue",
                          "reason": "该目录（含子目录）里没有 .cue 文件"},
                         ensure_ascii=False, indent=2))
    for c in cues:
        print(json.dumps(verify_cue(c, fetch=not a.no_net), ensure_ascii=False, indent=2))
