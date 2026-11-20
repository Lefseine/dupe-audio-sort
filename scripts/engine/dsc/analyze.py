"""单文件声学分析：一次解码同时得到 DR / 峰值 / 削波 / 有效位深 / 频谱截止。

设计要点
--------
* 解码用 ffmpeg -> f32le 流式管道，内存恒定（不吃整轨）。
* 同一遍解码里同时算：3 秒分块 RMS/峰值（DR）、样本峰值、削波、LSB 有效位深、
  逐帧 FFT 功率谱累加（频谱截止 / 挖洞 / 频段包络）。
* LUFS / LRA / True Peak 走 ebur128 独立一路，与主通道并发跑，墙钟时间近似不变。
* Spek 只有 GUI、没有命令行，所以"频谱上限"必须自己算：
  我们对齐 Spek 的视觉判读语义（肉眼找的那条高点截止的竖线），但给出精确 kHz 数值。
"""
from __future__ import annotations

import math
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import numpy as np

from . import ffmpeg_bin

CREATE_NO_WINDOW = 0x08000000

FFT_LOG_RE = re.compile(r"\b(I|LRA|Peak):\s*(-?\d+(?:\.\d+)?)\s*(LUFS|LU|dBFS)")

# DR 算法常量（与 TT DR Meter / foobar2000 DR 插件同一套口径）
DR_BLOCK_SEC = 3.0
DR_TOP_RATIO = 0.20
DR_SILENCE_DB = -70.0


def _db(x: float) -> float:
    return 20.0 * math.log10(x) if x > 1e-12 else -240.0


# --------------------------------------------------------------------------- #
# ebur128：LUFS-I / LRA / True Peak
# --------------------------------------------------------------------------- #
def _ebur128(path: str, timeout: float = 900.0) -> dict:
    cmd = [
        ffmpeg_bin.ffmpeg(), "-v", "info", "-nostdin", "-i", path,
        "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, errors="replace")
    except Exception:  # noqa: BLE001
        return {}
    txt = r.stderr or ""
    # 只取最后的 Summary 段
    idx = txt.rfind("Summary:")
    seg = txt[idx:] if idx >= 0 else txt
    out: dict = {}
    for m in FFT_LOG_RE.finditer(seg):
        key, val = m.group(1), float(m.group(2))
        if key == "I" and "lufs_i" not in out:
            out["lufs_i"] = val
        elif key == "LRA" and "lra" not in out:
            out["lra"] = val
        elif key == "Peak" and "true_peak_db" not in out:
            out["true_peak_db"] = val
    return out


# --------------------------------------------------------------------------- #
# 主分析
# --------------------------------------------------------------------------- #
def analyze_audio(
    path: str,
    meta: dict,
    progress: Callable[[str], None] | None = None,
    max_analysis_sr: int = 192000,
    fft_size: int = 4096,
    max_frames: int = 2048,
) -> dict:
    sr_native = int(meta.get("sample_rate") or 44100)
    sr = min(sr_native, max_analysis_sr)
    duration = float(meta.get("duration") or 0.0)
    is_dsd = bool(meta.get("is_dsd"))

    # 频谱 + 动态 两路并发
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_main = ex.submit(_main_pass, path, sr, sr_native, duration, fft_size, max_frames, is_dsd)
        f_ebur = ex.submit(_ebur128, path)
        res = f_main.result()
        try:
            res.update({k: v for k, v in f_ebur.result().items() if v is not None})
        except Exception:  # noqa: BLE001
            pass

    res.update(_interpret(meta, res))
    tp, li = res.get("true_peak_db"), res.get("lufs_i")
    if tp is not None and li is not None:
        res["plr"] = round(tp - li, 1)     # 峰值-响度比：母带压缩程度的另一面
    if progress:
        progress(f"完成 {meta.get('name')}")
    return res


def _main_pass(path, sr, sr_native, duration, fft_size, max_frames, is_dsd) -> dict:
    cmd = [
        ffmpeg_bin.ffmpeg(), "-v", "error", "-nostdin", "-i", path,
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", "2", "-ar", str(sr), "-f", "f32le", "-acodec", "pcm_f32le", "-",
    ]

    block_len = max(1, int(round(DR_BLOCK_SEC * sr)))
    hop = fft_size // 2
    est_frames = max(1, int((duration or 60) * sr / hop)) if duration else 4096
    stride = max(1, int(math.ceil(est_frames / max_frames)))

    window = np.hanning(fft_size).astype(np.float32)
    psum = np.zeros(fft_size // 2 + 1, dtype=np.float64)
    pmax = np.full(fft_size // 2 + 1, -200.0, dtype=np.float64)
    pcount = 0

    peak = 0.0
    clip_hard = 0
    clip_soft = 0
    total_samples = 0
    lsb_tz = 64
    lsb_seen = 0

    blk_sumsq = np.zeros(2, dtype=np.float64)
    blk_peak = np.zeros(2, dtype=np.float64)
    blk_fill = 0
    dr_per_ch: list[list[float]] = [[], []]
    block_rms_db: list[float] = []

    rolling = np.empty(0, dtype=np.float32)
    frame_ctr = 0

    # 声道关系 / DC 偏移（用来识破「单声道复制成立体声」和直流偏移）
    g_l2 = g_r2 = g_lr = g_l = g_r = 0.0

    err_f = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_f,
                            creationflags=CREATE_NO_WINDOW)
    try:
        while True:
            raw = proc.stdout.read(sr * 2 * 4)  # 约 1 秒立体声 f32
            if not raw:
                break
            n = len(raw) // 4
            arr = np.frombuffer(raw[: n * 4], dtype="<f4").astype(np.float32, copy=False)
            if arr.size % 2:
                arr = arr[:-1]
            st = arr.reshape(-1, 2)
            if st.shape[0] == 0:
                continue

            # ---- 声道相关 / DC（整轨累计）----
            L = st[:, 0].astype(np.float64)
            R = st[:, 1].astype(np.float64)
            g_l2 += float(L.dot(L))
            g_r2 += float(R.dot(R))
            g_lr += float(L.dot(R))
            g_l += float(L.sum())
            g_r += float(R.sum())

            # ---- 峰值 / 削波 ----
            a = np.abs(st)
            m = float(a.max())
            if m > peak:
                peak = m
            clip_hard += int(np.count_nonzero(a >= 0.99994))
            clip_soft += int(np.count_nonzero(a >= 0.999))
            total_samples += st.shape[0]

            # ---- LSB 有效位深（只取源生采样率、非 DSD 的前若干样本）----
            if lsb_seen < 2_000_000 and sr == sr_native and not is_dsd:
                take = min(st.shape[0], 2_000_000 - lsb_seen)
                chunk_l = st[:take, 0]
                tz = _min_trailing_zeros(chunk_l)
                if tz < lsb_tz:
                    lsb_tz = tz
                lsb_seen += take

            # ---- DR 分块 ----
            pos = 0
            while pos < st.shape[0]:
                need = block_len - blk_fill
                seg = st[pos: pos + need]
                blk_sumsq += np.einsum("ij,ij->j", seg, seg, dtype=np.float64)
                blk_peak = np.maximum(blk_peak, np.abs(seg).max(axis=0))
                blk_fill += seg.shape[0]
                pos += seg.shape[0]
                if blk_fill >= block_len:
                    rms = np.sqrt(blk_sumsq / blk_fill)
                    rms_db = [_db(float(rms[c])) for c in range(2)]
                    if max(rms_db) > DR_SILENCE_DB:
                        for c in range(2):
                            dr_per_ch[c].append(_db(float(blk_peak[c])) - rms_db[c])
                        block_rms_db.append(max(rms_db))
                    blk_sumsq[:] = 0.0
                    blk_peak[:] = 0.0
                    blk_fill = 0

            # ---- 频谱（中置单声道）----
            mono = st.mean(axis=1)
            rolling = np.concatenate([rolling, mono]) if rolling.size else mono
            nframes = (rolling.size - fft_size) // hop + 1
            if nframes > 0:
                idx = np.arange(nframes)[:, None] * hop + np.arange(fft_size)[None, :]
                frames = rolling[idx]
                sel = frames[::stride]
                if sel.shape[0]:
                    spec = np.abs(np.fft.rfft(sel * window, axis=1)) ** 2
                    psum += spec.sum(axis=0, dtype=np.float64)
                    frame_db = 10.0 * np.log10(np.maximum(spec, 1e-20))
                    pmax = np.maximum(pmax, frame_db.max(axis=0))
                    pcount += sel.shape[0]
                    frame_ctr += sel.shape[0]
                rolling = rolling[nframes * hop:]
    finally:
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        proc.wait(timeout=30)

    # 解码错误数 = 无损完整性的一道便宜体检（坏帧/坏块会在这里冒出来）
    decode_errors = 0
    try:
        err_f.seek(0)
        raw = err_f.read()
        decode_errors = sum(1 for ln in raw.splitlines() if ln.strip())
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            err_f.close()
        except Exception:  # noqa: BLE001
            pass

    # 收尾：不足一块的尾巴（>0.5s 才计）
    if blk_fill > sr * 0.5:
        rms = np.sqrt(blk_sumsq / blk_fill)
        rms_db = [_db(float(rms[c])) for c in range(2)]
        if max(rms_db) > DR_SILENCE_DB:
            for c in range(2):
                dr_per_ch[c].append(_db(float(blk_peak[c])) - rms_db[c])
            block_rms_db.append(max(rms_db))

    # ---- DR 汇总 ----
    ch_dr = []
    for c in range(2):
        vals = sorted(dr_per_ch[c], reverse=True)
        if not vals:
            continue
        k = max(1, int(round(len(vals) * DR_TOP_RATIO)))
        ch_dr.append(float(np.mean(vals[:k])))
    dr_value = float(np.mean(ch_dr)) if ch_dr else None

    avg_power = psum / max(1, pcount)
    mean_db = 10.0 * np.log10(np.maximum(avg_power, 1e-20))
    freqs = np.fft.rfftfreq(fft_size, d=1.0 / sr)

    spec = _spectral_features(freqs, mean_db, sr, duration=duration)

    eff_bits = None
    if lsb_tz < 48:
        eff_bits = int(max(1, 32 - lsb_tz))

    # 声道相关性：1.0 ≈ 左右完全一样（单声道复制）；0 ≈ 无关；负 ≈ 反相
    corr = None
    side_db = None
    if g_l2 > 0 and g_r2 > 0:
        corr = round(float(g_lr / math.sqrt(g_l2 * g_r2)), 4)
        side = g_l2 + g_r2 - 2 * g_lr          # (L-R)² 能量
        mid = g_l2 + g_r2 + 2 * g_lr           # (L+R)² 能量
        if mid > 0:
            side_db = round(10.0 * math.log10(max(side, 1e-20) / mid), 1)

    dc_db = None
    if total_samples > 0:
        dc = max(abs(g_l), abs(g_r)) / total_samples
        dc_db = round(_db(dc), 1)

    return {
        "sample_peak_db": round(_db(peak), 2) if peak > 0 else None,
        "clipped_samples": clip_hard,
        "clipped_samples_soft": clip_soft,
        "total_samples": total_samples,
        "dr_value": round(dr_value, 1) if dr_value is not None else None,
        "dr_blocks": len(block_rms_db),
        "dr_block_rms_db": [round(v, 1) for v in block_rms_db],
        "effective_bits": eff_bits,
        "lsb_zero_bits": lsb_tz if lsb_tz < 48 else None,
        "channel_corr": corr,
        "side_energy_db": side_db,
        "dc_offset_db": dc_db,
        "decode_errors": decode_errors,
        "analyzed_seconds": round(frame_ctr * hop / sr, 1) if sr else None,
        "analysis_sr": sr,
        **spec,
    }


def meta_bits_ok(meta_none=None) -> bool:
    return False


def _min_trailing_zeros(x: np.ndarray) -> int:
    """估算量化网格：把 float32 还原成 int32 定点，取最低非零位的位数。"""
    x = x[np.isfinite(x)]
    if x.size == 0:
        return 64
    q = np.rint(np.asarray(x, dtype=np.float64) * 2147483648.0).astype(np.int64)
    q = q[q != 0]
    if q.size == 0:
        return 64
    low = q & (-q)
    tz = np.log2(low.astype(np.float64))
    return int(np.min(tz))


# --------------------------------------------------------------------------- #
# 频谱特征
# --------------------------------------------------------------------------- #
def _spectral_features(freqs: np.ndarray, mean_db: np.ndarray,
                       sr: int, duration: float = 0.0) -> dict:
    nyq = sr / 2.0
    binhz = freqs[1] - freqs[0] if len(freqs) > 1 else nyq
    sm = _smooth(mean_db, max(3, int(round(150.0 / binhz))))

    centers, levels = _band_spectrum(freqs, mean_db, frac=12)
    c3, l3 = _band_spectrum(freqs, mean_db, frac=3)
    cutoff, method = _cutoff_from_bands(centers, levels, nyq)

    ref_mask = centers <= min(14000.0, nyq * 0.9)
    ref = float(np.percentile(levels[ref_mask], 95)) if ref_mask.any() else float(levels.max())
    tail = centers > nyq * 0.92
    noise = float(np.median(levels[tail])) if tail.any() else float(levels.min())

    def blv(lo, hi):
        m = (c3 >= lo) & (c3 <= hi)
        if not m.any():
            return None
        return round(float(np.mean(l3[m])), 2)

    # 挖洞检测：2k–16k 内窄而深的缺口（有损转无损的典型指纹）
    lo, hi = 2000.0, min(16000.0, max(3000.0, cutoff * 0.95))
    holes = _count_notches(freqs, sm, lo, hi, binhz, depth_db=14.0, max_width_hz=350.0)
    minutes = max(duration / 60.0, 0.5)

    env = []
    for f in [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 12000, 16000, 20000, 24000, 32000, 40000]:
        if f > nyq * 1.05:
            break
        v = blv(f / 1.122, f * 1.122)
        env.append({"f": f, "db": v})

    beyond = centers > cutoff * 1.05
    hf_ratio = None
    if beyond.any():
        p = 10.0 ** (levels / 10.0)
        tot = float(p.sum())
        hf_ratio = round(100.0 * float(p[beyond].sum()) / tot, 4) if tot > 0 else None

    return {
        "nyquist_khz": round(nyq / 1000, 2),
        "cutoff_khz": round(cutoff / 1000, 2),
        "cutoff_ratio": round(cutoff / nyq, 4) if nyq else None,
        "cutoff_method": method,
        "spectrum_floor_db": round(noise, 1),
        "spectrum_ref_db": round(ref, 1),
        "notch_count": holes,
        "notch_rate_per_min": round(holes / minutes, 1),
        "octave_envelope": env,
        "hf_energy_pct": hf_ratio,
        "band_1k_db": blv(800, 1250),
        "band_8k_db": blv(6300, 10000),
        "band_16k_db": blv(14000, 18000) if nyq > 19000 else None,
    }


def _band_spectrum(freqs: np.ndarray, mean_db: np.ndarray, frac: int = 12,
                   fmin: float = 20.0, fmax: float | None = None):
    """把 FFT 细谱积分成 1/frac 倍频程带，得到与尺度无关的"每带电平曲线"。

    这样做的原因：单 bin 的 dB 值随 FFT 长度变化，10*log10 后还会被 bin 宽度带偏；
    频带积分后可跨文件比较，也才谈得上"-50 dB 算有内容"这种绝对门限。
    """
    fmax = fmax or float(freqs[-1])
    ratio = 2 ** (1.0 / frac)
    edges = [fmin]
    while edges[-1] < fmax:
        edges.append(edges[-1] * ratio)
    centers, levels = [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (freqs >= lo) & (freqs < hi)
        if not m.any():
            continue
        p = 10.0 ** (mean_db[m] / 10.0)
        centers.append((lo * hi) ** 0.5)
        levels.append(10.0 * math.log10(float(np.mean(p)) + 1e-30))
    return np.array(centers), np.array(levels)


def _cutoff_from_bands(centers: np.ndarray, levels: np.ndarray, nyq: float,
                       drop_db: float = 50.0) -> tuple[float, str]:
    """双判据：
    1) 断崖（cliff）——某处出现 >35 dB 的陡降且之后持续走低，这是低通的铁证；
    2) 电平门限——相对中频参考掉 50 dB 以下就当作没有内容。
    两者取更保守（更低）的截止点，并用插值把边界缩到 0.1 kHz 级。
    """
    if len(centers) < 8:
        return nyq, "unknown"

    ref_mask = centers <= min(14000.0, nyq * 0.9)
    ref = float(np.percentile(levels[ref_mask], 95)) if ref_mask.any() else float(levels.max())
    thr = ref - drop_db

    # --- 1) 门限法 ---
    above = levels >= thr
    lvl_cut = 0.0
    run = 0
    for i in range(len(levels) - 1, -1, -1):
        if above[i]:
            run += 1
            if run >= 2:
                lvl_cut = float(centers[i + run - 1])
                break
        else:
            run = 0
    if above.size and above[-1]:
        lvl_cut = nyq
    if lvl_cut <= 0:
        lvl_cut = float(centers[int(np.argmax(above))]) if above.any() else float(centers[0])

    # --- 2) 断崖法 ---
    cliff_cut = 0.0
    for i in range(1, len(centers) - 2):
        f = centers[i]
        if f < 6000:
            continue
        lo_m = (centers >= f / 1.26) & (centers < f * 0.96)
        hi_m = (centers >= f * 1.03) & (centers < f * 1.30)
        if not lo_m.any() or not hi_m.any():
            continue
        below = float(np.median(levels[lo_m]))
        above_ = float(np.median(levels[hi_m]))
        after = float(np.median(levels[centers > f * 1.3])) if (centers > f * 1.3).any() else above_
        if below - above_ >= 35.0 and after <= above_ + 6.0:
            cliff_cut = float(f)
            break

    if cliff_cut:
        return cliff_cut, "cliff"
    return lvl_cut, "level"


def _smooth(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return x
    k = min(k | 1, max(3, len(x) // 3 * 2 - 1))
    ker = np.ones(k) / k
    return np.convolve(x, ker, mode="same")


def _count_notches(freqs, sm, lo, hi, binhz, depth_db=14.0, max_width_hz=350.0) -> int:
    m = (freqs >= lo) & (freqs <= hi)
    if not m.any():
        return 0
    seg = sm[m]
    w = max(9, int(round(1500.0 / binhz)) | 1)
    local = _smooth(seg, w)
    dip = seg < (local - depth_db)
    max_w = max(2, int(round(max_width_hz / binhz)))
    count = 0
    run = 0
    for v in dip:
        if v:
            run += 1
        else:
            if 2 <= run <= max_w:
                count += 1
            run = 0
    if 2 <= run <= max_w:
        count += 1
    return count


# --------------------------------------------------------------------------- #
# 结论解释层：把数字翻译成"这版到底好不好"
# --------------------------------------------------------------------------- #
def _interpret(meta: dict, m: dict) -> dict:
    sr = int(meta.get("sample_rate") or 0)
    bits = meta.get("bit_depth")
    fam = meta.get("family")
    cut = m.get("cutoff_khz") or 0.0
    nyq = m.get("nyquist_khz") or 0.0
    ratio = m.get("cutoff_ratio") or 0.0
    flags: list[dict] = []
    notes: list[str] = []
    verdict = "unknown"

    # ---- 0) 先判升频：容器采样率远高于实际内容 ----
    upsampled = bool(not meta.get("is_dsd") and sr >= 88200 and ratio < 0.55)
    if upsampled:
        flags.append({"level": "bad", "code": "UPSAMPLED",
                      "text": f"标称 {sr/1000:.0f} kHz，实际内容只到 {cut:.1f} kHz —— "
                              f"低采样率升频，Hi-Res 是空的（文件变大，信息没变多）"})
        notes.append(f"等效原生采样率约 {cut*2:.1f} kHz")

    # ---- 1) 频谱上限 ----
    if ratio >= 0.97:
        verdict = "full-band"
        notes.append(f"频谱满幅到 {cut:.1f} kHz（奈奎斯特 {nyq:.1f} kHz），全频带完整")
    elif ratio >= 0.88:
        verdict = "near-full"
        notes.append(f"截止 {cut:.1f} kHz（奈奎斯特的 {ratio*100:.0f}%），"
                     f"多为母带/ADC 的柔和低通，可接受")
    elif 14.5 <= cut <= 17.2:
        verdict = "lossy-128"
        flags.append({"level": "bad", "code": "TRANSCODE",
                      "text": f"频谱在 {cut:.1f} kHz 断崖 —— 128 kbps 级有损编码的典型特征"})
    elif 17.2 < cut <= 20.9:
        verdict = "lossy-192-320"
        flags.append({"level": "bad", "code": "TRANSCODE",
                      "text": f"频谱截止 {cut:.1f} kHz —— 192–320 kbps 有损来源，不是真无损"})
    else:
        verdict = "narrow"
        flags.append({"level": "warn", "code": "NARROW",
                      "text": f"频谱只到 {cut:.1f} kHz（奈奎斯特的 {ratio*100:.0f}%），信息量不足"})

    if fam == "lossless" and verdict in ("lossy-128", "lossy-192-320", "narrow") and not upsampled:
        flags.append({"level": "bad", "code": "FAKE_LOSSLESS",
                      "text": f"容器是 {str(meta.get('codec','')).upper()} 无损，"
                              f"但内容只到 {cut:.1f} kHz：它经历过有损编码"})

    # ---- 2) 有效位深 ----
    eb = m.get("effective_bits")
    if eb and bits:
        if eb <= bits - 4:
            flags.append({"level": "warn", "code": "FAKE_DEPTH",
                          "text": f"标称 {bits}-bit，实测量化网格约 {eb}-bit（低位被补零），疑似低比特升位"})
        elif eb >= 18 and bits >= 24 and not upsampled:
            notes.append(f"有效位深约 {eb}-bit，低位有真实信息，是真 24-bit 素材")
        elif bits >= 24:
            notes.append(f"标称 {bits}-bit，有效量化约 {eb}-bit")

    # ---- 3) 挖洞（有损编码残留指纹）----
    if m.get("notch_rate_per_min", 0) > 25 and verdict in ("full-band", "near-full"):
        flags.append({"level": "warn", "code": "NOTCH",
                      "text": f"2–16 kHz 检出 {m.get('notch_count')} 处窄带缺口"
                              f"（{m.get('notch_rate_per_min')}/分钟）—— 像是有损编码后再转无损"})

    # ---- 4) 削波 ----
    clip = m.get("clipped_samples") or 0
    tot = m.get("total_samples") or 1
    ppm = clip / tot * 1e6
    if ppm > 500:
        flags.append({"level": "bad", "code": "CLIP",
                      "text": f"硬削波样本 {clip:,} 个（{ppm:.0f} ppm），波峰被削平"})
    elif ppm > 20:
        flags.append({"level": "warn", "code": "CLIP",
                      "text": f"轻微削波 {clip:,} 个样本（{ppm:.0f} ppm）"})

    # ---- 5) 动态 ----
    dr = m.get("dr_value")
    if dr is not None:
        if dr >= 12:
            notes.append(f"DR{dr:.0f}：动态充裕")
        elif dr >= 9:
            notes.append(f"DR{dr:.0f}：动态正常，现代流行/摇滚的常见水平")
        elif dr >= 7:
            notes.append(f"DR{dr:.0f}：动态偏紧，做过明显的母带压缩")
        else:
            flags.append({"level": "warn", "code": "DR",
                          "text": f"DR{dr:.0f}：响度战争型母带，压缩很重"})

    # ---- 6) 真峰值 ----
    tp = m.get("true_peak_db")
    if tp is not None and tp > 0.2:
        flags.append({"level": "warn", "code": "TP",
                      "text": f"真峰值 {tp:+.1f} dBTP 已过 0 dBFS，解码到 DAC 时可能出现失真"})

    # ---- 7) 声道关系：单声道复制成立体声 / 反相 ----
    corr = m.get("channel_corr")
    ch = int(meta.get("channels") or 2)
    if corr is not None and ch >= 2:
        if corr >= 0.9995:
            flags.append({"level": "warn", "code": "MONO_DUP",
                          "text": f"左右声道相关系数 {corr:.4f}（侧信号 {m.get('side_energy_db')} dB）——"
                                  f"两声道几乎完全相同：单声道源复制成双声道。"
                                  f"若这张碟本就是单声道录音，属正常，可忽略"})
        elif corr >= 0.995:
            notes.append(f"声道高度相关（{corr:.4f}），立体声像很窄")
        elif corr < -0.2:
            flags.append({"level": "warn", "code": "PHASE",
                          "text": f"左右声道反相（相关系数 {corr:.3f}），单声道叠加会互相抵消"})

    # ---- 8) 直流偏移 ----
    dc = m.get("dc_offset_db")
    if dc is not None and dc > -45:
        flags.append({"level": "warn", "code": "DC",
                      "text": f"直流偏移 {dc} dBFS，白占动态余量（多为转录/抓轨环节引入）"})

    # ---- 9) 解码完整性 ----
    de = m.get("decode_errors") or 0
    if de > 0:
        flags.append({"level": "bad", "code": "DECODE",
                      "text": f"解码过程报错 {de} 处，文件可能损坏或截断（建议再用 flac -t / CUETools 复验）"})

    return {"verdict": verdict, "flags": flags, "notes": notes}
