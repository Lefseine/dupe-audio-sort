"""元数据读取：统一用 ffprobe JSON，覆盖面比 mutagen 广（APE/DSF/WMA/MKA 都能读）。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import ffmpeg_bin

CREATE_NO_WINDOW = 0x08000000

AUDIO_EXT = {
    ".flac", ".wav", ".wave", ".ape", ".wv", ".aif", ".aiff", ".alac", ".m4a", ".aac",
    ".mp3", ".ogg", ".oga", ".opus", ".wma", ".dsf", ".dff", ".tta", ".mka", ".caf", ".au",
}

_CODEC_FAMILY = {
    "flac": "lossless", "alac": "lossless", "ape": "lossless", "wavpack": "lossless",
    "tta": "lossless", "truehd": "lossless", "pcm_s16le": "lossless", "pcm_s24le": "lossless",
    "pcm_s32le": "lossless", "pcm_f32le": "lossless", "pcm_s16be": "lossless",
    "pcm_s24be": "lossless", "pcm_s32be": "lossless", "wmalossless": "lossless",
    "mp3": "lossy", "aac": "lossy", "vorbis": "lossy", "opus": "lossy", "wmav2": "lossy",
    "ac3": "lossy", "mp2": "lossy", "dts": "lossy",
    "dsd_lsbf": "dsd", "dsd_msbf": "dsd", "dsd_lsbf_planar": "dsd", "dsd_msbf_planar": "dsd",
}


def is_audio(path: str | Path) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXT


def probe(path: str | Path) -> dict:
    cmd = [
        ffmpeg_bin.ffprobe(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-select_streams", "a:0", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {(r.stderr or '').strip()[:200]}")

    j = json.loads(r.stdout or "{}")
    st = (j.get("streams") or [{}])[0]
    fmt = j.get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    st_tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
    for k, v in st_tags.items():
        tags.setdefault(k, v)

    codec = (st.get("codec_name") or "").lower()
    sr = int(st.get("sample_rate") or 0) or None
    bits = st.get("bits_per_raw_sample") or st.get("bits_per_sample") or None
    try:
        bits = int(bits) if bits not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        bits = None

    duration = _f(fmt.get("duration")) or _f(st.get("duration")) or 0.0
    size = int(_f(fmt.get("size")) or Path(path).stat().st_size)
    bitrate = _f(fmt.get("bit_rate")) or _f(st.get("bit_rate"))
    if not bitrate and duration > 0:
        bitrate = size * 8 / duration

    family = _CODEC_FAMILY.get(codec, "unknown")
    if codec.startswith("pcm_"):
        family = "lossless"

    return {
        "path": str(path),
        "name": Path(path).name,
        "folder": str(Path(path).parent),
        "ext": Path(path).suffix.lower(),
        "format_name": fmt.get("format_name", ""),
        "codec": codec,
        "family": family,
        "sample_rate": sr,
        "bit_depth": bits,
        "channels": int(st.get("channels") or 0) or None,
        "channel_layout": st.get("channel_layout") or "",
        "duration": round(duration, 3),
        "size_bytes": size,
        "bitrate_kbps": round(bitrate / 1000, 1) if bitrate else None,
        "title": tags.get("title") or "",
        "artist": tags.get("artist") or tags.get("album_artist") or tags.get("performer") or "",
        "album": tags.get("album") or "",
        "album_artist": tags.get("album_artist") or "",
        "track": tags.get("track") or "",
        "date": tags.get("date") or tags.get("year") or "",
        "genre": tags.get("genre") or "",
        "comment": tags.get("comment") or "",
        "is_dsd": family == "dsd" or Path(path).suffix.lower() in (".dsf", ".dff"),
    }


def _f(v) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None
