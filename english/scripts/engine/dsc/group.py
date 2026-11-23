"""同曲识别、版本聚类与打分。

关键判断：什么算"同一首歌的不同版本"
  title 归一化相等 + 艺人相近 + 时长相近 + 版本标签同类（Live/Remix 不混进来）
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

try:
    from rapidfuzz import fuzz
except Exception:  # noqa: BLE001
    fuzz = None

BRACKET_RE = re.compile(r"[\(\[（【][^\)\]）】]*[\)\]）】]")
FEAT_RE = re.compile(r"\b(feat|ft|featuring|with)\b\.?\s.*$", re.I)
DASH_TAIL_RE = re.compile(r"\s[-–—]\s.*$")

VERSION_TAGS = {
    "live": ["live", "现场", "演唱会", "concert", "unplugged session"],
    "remix": ["remix", "混音", "mixes", "rmx", "dj", "club"],
    "instrumental": ["instrumental", "伴奏", "纯音乐", "karaoke", "off vocal", "inst"],
    "acoustic": ["acoustic", "不插电", "unplugged"],
    "demo": ["demo", "小样", "rough"],
    "edit": ["edit", "radio edit", "short version", "剪辑"],
    "mono": ["mono", "单声道"],
    "alt": ["alternate", "alternative take", "outtake", "版本二", "alt take"],
}

QUALITY_WORDS = {
    "remaster": ["remaster", "remastered", "复刻", "重制", "重新母带"],
    "hires": ["hi-res", "hires", "24bit", "24-96", "96k", "dsd", "sacd", "mqa"],
    "collector": ["deluxe", "special edition", "anniversary", "纪念", "限量", "首版", "japan", "日版"],
}


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return s.strip()


TRACK_NO_RE = re.compile(r"^\s*(?:cd\s*\d+\s*[-_.]?\s*)?\d{1,3}\s*[-_.\s]\s*", re.I)
QUALITY_STRIP_RE = re.compile(
    r"\b(?:hq|hqcd|xrcd|hires|hi-res|24\s*-?\s*\d{2,3}|16\s*-?\s*\d{2,3}|"
    r"\d{2,3}\s*(?:bit|k|khz)|dsd\d*|sacd|mqa|flac|ape|wav|remaster(?:ed)?|"
    r"deluxe|special edition|anniversary|japan|shm)\b", re.I)
ZH_QUALITY_WORDS = ["无损", "高解析", "母带", "复刻", "首版", "重制", "重新母带", "纪念版",
                    "限量版", "日版", "港版", "台版", "引进版", "发烧", "试音", "抓轨"]


def title_key(s: str) -> str:
    s = _fold(s)
    s = BRACKET_RE.sub(" ", s)
    s = FEAT_RE.sub(" ", s)
    s = TRACK_NO_RE.sub(" ", s)
    s = QUALITY_STRIP_RE.sub(" ", s)
    for w in ZH_QUALITY_WORDS:
        s = s.replace(w, " ")
    s = s.lower()
    s = re.sub(r"[^\w\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", "", s)
    return s


def artist_key(s: str) -> str:
    s = _fold(s).lower()
    s = re.split(r"[;&/、,，]|feat\.?|ft\.?", s)[0]
    s = re.sub(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]+", "", s)
    return s


def version_class(text: str) -> str:
    t = _fold(text).lower()
    for cls, words in VERSION_TAGS.items():
        if any(w in t for w in words):
            return cls
    return "studio"


def quality_tags(text: str) -> list[str]:
    t = _fold(text).lower()
    return [k for k, ws in QUALITY_WORDS.items() if any(w in t for w in ws)]


def _title_from_name(name: str) -> str:
    stem = Path(name).stem
    stem = TRACK_NO_RE.sub(" ", stem)
    return stem.strip()


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 100.0
    if a == b:
        return 100.0
    if fuzz is None:
        return 100.0 if a in b or b in a else 0.0
    return float(fuzz.token_set_ratio(a, b))


def quick_hash(path: str, limit: int = 1 << 20) -> str:
    h = hashlib.blake2b(digest_size=8)
    try:
        with open(path, "rb") as f:
            h.update(f.read(limit))
    except OSError:
        return ""
    return h.hexdigest()


# --------------------------------------------------------------------------- #
def build_groups(items: list[dict], duration_tol_sec: float = 8.0,
                 duration_tol_ratio: float = 0.05) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for it in items:
        t = it.get("title") or _title_from_name(it["name"])
        attr = f"{t} {it.get('album','')} {it.get('name','')}"
        it["_title_key"] = title_key(t) or title_key(Path(it["name"]).stem)
        it["_artist_key"] = artist_key(it.get("artist") or it.get("album_artist") or "")
        it["_vclass"] = version_class(attr)
        it["_qtags"] = quality_tags(attr)
        it["_qhash"] = quick_hash(it["path"])
        buckets.setdefault(it["_title_key"], []).append(it)

    groups: list[dict] = []
    for tkey, lst in buckets.items():
        for acl in _cluster_artist(lst):
            for dcl in _cluster_duration(acl, duration_tol_sec, duration_tol_ratio):
                groups.append({"key": tkey, "versions": dcl})

    groups.sort(key=lambda g: (-len(g["versions"]), _title_display(g)))
    return groups


def _cluster_artist(lst: list[dict]) -> list[list[dict]]:
    clusters: list[list[dict]] = []
    for it in lst:
        placed = False
        for c in clusters:
            if all(_sim(it["_artist_key"], o["_artist_key"]) >= 78 for o in c):
                c.append(it)
                placed = True
                break
        if not placed:
            clusters.append([it])
    return clusters


def _cluster_duration(lst: list[dict], tol: float, ratio: float) -> list[list[dict]]:
    out: list[list[dict]] = []
    for it in sorted(lst, key=lambda x: x.get("duration") or 0):
        d = it.get("duration") or 0
        placed = False
        for c in out:
            base = c[0].get("duration") or 0
            if base <= 0 or d <= 0:
                continue
            if abs(d - base) <= max(tol, ratio * base):
                c.append(it)
                placed = True
                break
        if not placed:
            out.append([it])
    # 版本类别不同 -> 再拆开
    final: list[list[dict]] = []
    for c in out:
        by_v: dict[str, list[dict]] = {}
        for it in c:
            by_v.setdefault(it["_vclass"], []).append(it)
        final.extend(by_v.values())
    return final


def _title_display(g: dict) -> str:
    for v in g["versions"]:
        if v.get("title"):
            return v["title"]
    return Path(g["versions"][0]["path"]).stem


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #
WEIGHTS = {"spectrum": 35, "depth": 20, "dynamics": 25, "headroom": 10, "clean": 10}


def score_version(v: dict) -> dict:
    m = v
    codes = {f["code"] for f in v.get("flags", [])}
    ratio = m.get("cutoff_ratio") or 0.0
    sr = v.get("sample_rate") or 44100
    bits = v.get("bit_depth") or 16
    dr = v.get("dr_value")
    tp = v.get("true_peak_db")
    lufs = v.get("lufs_i")

    # 1. 频谱真实度
    if ratio >= 0.97:
        spec = WEIGHTS["spectrum"]
    elif ratio >= 0.92:
        spec = WEIGHTS["spectrum"] * 0.86
    elif ratio >= 0.88:
        spec = WEIGHTS["spectrum"] * 0.68
    elif ratio >= 0.78:
        spec = WEIGHTS["spectrum"] * 0.40
    elif ratio >= 0.70:
        spec = WEIGHTS["spectrum"] * 0.22
    else:
        spec = WEIGHTS["spectrum"] * 0.08
    if "NOTCH" in codes:
        spec -= 6
    if "FAKE_LOSSLESS" in codes:
        spec -= 8
    spec = max(0.0, spec)

    # 2. 位深 / 采样率真实性
    depth = {16: 11.0, 20: 12.5, 24: 15.0, 32: 15.0}.get(int(bits), 11.0)
    if sr >= 88200:
        depth += 5.0
    elif sr >= 48000:
        depth += 3.5
    else:
        depth += 2.5
    if "UPSAMPLED" in codes:
        depth -= 11
    if "FAKE_DEPTH" in codes:
        depth -= 6
    depth = max(0.0, min(WEIGHTS["depth"], depth))

    # 3. 动态
    if dr is None:
        dyn = WEIGHTS["dynamics"] * 0.45
    else:
        dyn = min(dr, 16.0) / 16.0 * WEIGHTS["dynamics"]

    # 4. 峰值余量 PLR
    if tp is not None and lufs is not None:
        plr = tp - lufs
        head = max(0.0, min(WEIGHTS["headroom"], (plr - 6.0) / 12.0 * WEIGHTS["headroom"]))
    else:
        head = WEIGHTS["headroom"] * 0.5

    # 5. 干净度（削波）
    clip_ppm = (v.get("clipped_samples") or 0) / max(1, v.get("total_samples") or 1) * 1e6
    clean = WEIGHTS["clean"] * (1.0 - min(1.0, clip_ppm / 800.0))
    if "TP" in codes:
        clean *= 0.85

    total = spec + depth + dyn + head + clean
    return {
        "score": round(total, 1),
        "breakdown": {
            "Spectrum authenticity": round(spec, 1),
            "Bit depth / sample rate": round(depth, 1),
            "Dynamic DR": round(dyn, 1),
            "Peak headroom": round(head, 1),
            "No clipping": round(clean, 1),
        },
    }


def rank_group(g: dict) -> dict:
    vs = g["versions"]
    for v in vs:
        v.update(score_version(v))

    # 完全相同的文件副本合并标记
    seen: dict[str, dict] = {}
    for v in vs:
        h = v.get("_qhash")
        if h and h in seen:
            v["duplicate_of"] = seen[h]["path"]
        elif h:
            seen[h] = v

    vs.sort(key=lambda v: (-(v.get("score") or 0), v.get("size_bytes") or 0))
    best = vs[0] if vs else None
    for i, v in enumerate(vs, 1):
        v["rank"] = i
        v["is_best"] = i == 1

    g["title"] = _title_display(g)
    g["best"] = best
    g["spread"] = {
        "cutoff_khz": [min((v.get('cutoff_khz') or 0) for v in vs), max((v.get('cutoff_khz') or 0) for v in vs)],
        "dr": [min((v.get('dr_value') or 99) for v in vs), max((v.get('dr_value') or 0) for v in vs)],
        "score": [min((v.get('score') or 0) for v in vs), max((v.get('score') or 0) for v in vs)],
    }
    g["advice"] = _advice(g)
    return g


def _advice(g: dict) -> str:
    vs = g["versions"]
    best = vs[0]
    if len(vs) == 1:
        bad = [f for f in best.get("flags", []) if f["level"] == "bad"]
        if bad:
            return f"Only one version found, but {best.get('name')} has issues: " + "; ".join(f["text"] for f in bad[:2])
        return "Only one version; no side-by-side comparison possible"

    lines = [
        f"Keep #{best.get('rank')}. {Path(best['name']).name}"
        f"（{best.get('album') or best.get('folder')}）: "
        f"cutoff {best.get('cutoff_khz')} kHz / DR{best.get('dr_value')} / "
        f"effective {best.get('effective_bits') or '?'} bit / overall {best.get('score')}"
    ]
    worst = vs[-1]
    if (worst.get("score") or 0) + 8 < (best.get("score") or 0):
        lines.append(f"Drop first: {Path(worst['name']).name} (overall {worst.get('score')}, "
                     f"cutoff {worst.get('cutoff_khz')} kHz / DR{worst.get('dr_value')})")
    flags = [f["text"] for f in best.get("flags", []) if f["level"] == "bad"]
    if flags:
        lines.append("But note: " + flags[0])
    return "; ".join(lines)
