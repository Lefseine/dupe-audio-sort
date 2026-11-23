"""导出：自包含 HTML 报告 + CSV 明细。"""
from __future__ import annotations

import csv
import html
import time
from pathlib import Path

LEVEL_COLOR = {"bad": "#e5484d", "warn": "#f5a524", "ok": "#30a46c"}


def write_reports(res: dict, outdir: Path, jid: str) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    h = outdir / f"{jid}.html"
    c = outdir / f"{jid}.csv"
    h.write_text(render_html(res), encoding="utf-8")
    write_csv(res, c)
    return {"html": str(h), "csv": str(c)}


def write_csv(res: dict, path: Path) -> None:
    cols = ["Song", "Rank", "Recommended", "Overall", "Filename", "Album", "Artist", "Duration s", "Format",
            "Bitrate kbps", "Sample rate Hz", "Bit depth", "Effective bits", "Cutoff kHz", "Nyquist kHz",
            "Spectrum ratio", "DR", "LUFS", "LRA", "True peak dBTP", "PLR", "Clipped samples", "Notches/min",
            "Channel corr", "DC offset dBFS", "Decode errors", "Path"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for g in res.get("groups", []):
            for v in g.get("versions", []):
                w.writerow([
                    g.get("title"), v.get("rank"), "★" if v.get("is_best") else "",
                    v.get("score"), v.get("name"), v.get("album"), v.get("artist"),
                    v.get("duration"), v.get("codec"), v.get("bitrate_kbps"),
                    v.get("sample_rate"), v.get("bit_depth"), v.get("effective_bits"),
                    v.get("cutoff_khz"), v.get("nyquist_khz"), v.get("cutoff_ratio"),
                    v.get("dr_value"), v.get("lufs_i"), v.get("lra"),
                    v.get("true_peak_db"), v.get("plr"), v.get("clipped_samples"),
                    v.get("notch_rate_per_min"), v.get("channel_corr"), v.get("dc_offset_db"),
                    v.get("decode_errors"), v.get("path"),
                ])


def _bar(val: float, maxv: float, color: str = "#5b8def") -> str:
    pct = 0 if not maxv else max(2, min(100, val / maxv * 100))
    return (f'<span class="bar"><i style="width:{pct:.0f}%;background:{color}"></i></span>')


def render_html(res: dict) -> str:
    st = res.get("stats", {})
    groups = res.get("groups", [])
    multi = [g for g in groups if len(g.get("versions", [])) > 1]

    rows = []
    for g in multi:
        cards = []
        for v in g["versions"]:
            flag_html = "".join(
                f'<div class="flag {f["level"]}">{html.escape(f["text"])}</div>'
                for f in v.get("flags", [])
            )
            rows_bd = "".join(
                f'<span class="bd">{html.escape(k)} <b>{val}</b></span>'
                for k, val in (v.get("breakdown") or {}).items()
            )
            cards.append(f"""
            <div class="ver {'best' if v.get('is_best') else ''}">
              <div class="vhead">
                <span class="rk">#{v.get('rank')}</span>
                <span class="sc">{v.get('score')}</span>
                {'<span class="tag">Recommended</span>' if v.get('is_best') else ''}
              </div>
              <div class="fname">{html.escape(str(v.get('name')))}</div>
              <div class="album">{html.escape(str(v.get('album') or ''))}
                  <span class="dim">{html.escape(str(v.get('folder')))}</span></div>
              <table class="kv">
                <tr><td>Spectral cutoff</td><td><b>{v.get('cutoff_khz')}</b> kHz
                    <span class="dim">/ Nyquist {v.get('nyquist_khz')} kHz ({round((v.get('cutoff_ratio') or 0)*100)}%)</span></td></tr>
                <tr><td>Dynamic DR</td><td><b>{v.get('dr_value')}</b>
                    {_bar(v.get('dr_value') or 0, 20, '#30a46c')}</td></tr>
                <tr><td>LUFS / LRA</td><td>{v.get('lufs_i')} / {v.get('lra')}</td></tr>
                <tr><td>True peak</td><td>{v.get('true_peak_db')} dBTP</td></tr>
                <tr><td>Rate / bits</td><td>{v.get('sample_rate')} Hz / {v.get('bit_depth')} bit
                    <span class="dim">(measured effective {v.get('effective_bits')} bit)</span></td></tr>
                <tr><td>Codec</td><td>{html.escape(str(v.get('codec') or ''))} · {v.get('bitrate_kbps')} kbps</td></tr>
                <tr><td>PLR</td><td>{v.get('plr')} dB
                    <span class="dim">(peak/loudness; lower means more loudness-war-like)</span></td></tr>
                <tr><td>Channel correlation</td><td>{v.get('channel_corr')}
                    <span class="dim">(≈1 means mono duplicate / extremely narrow stereo image)</span></td></tr>
                <tr><td>Clipping</td><td>{v.get('clipped_samples')} samples
                    <span class="dim">· {v.get('decode_errors')} decode errors · DC {v.get('dc_offset_db')} dBFS</span></td></tr>
              </table>
              <div class="bdrow">{rows_bd}</div>
              {flag_html}
            </div>""")
        rows.append(f"""
        <section class="group">
          <h2>{html.escape(str(g.get('title')))} <span class="cnt">{len(g['versions'])} versions</span></h2>
          <p class="advice">{html.escape(str(g.get('advice')))}</p>
          <div class="vers">{''.join(cards)}</div>
        </section>""")

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>DupeScope — Same-Track Version Comparison Report</title>
<style>
:root{{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e8ee;--dim:#8b93a5;--acc:#5b8def}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;padding:32px}}
h1{{font-size:22px;margin:0 0 4px}}
.sub{{color:var(--dim);margin-bottom:20px}}
.kpis{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:26px}}
.kpi{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:120px}}
.kpi b{{display:block;font-size:20px}}
.kpi span{{color:var(--dim);font-size:12px}}
.group{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px}}
.group h2{{font-size:17px;margin:0 0 6px}}
.cnt{{font-size:12px;color:var(--dim);font-weight:400;margin-left:6px}}
.advice{{color:#f5d90a;font-size:13px;margin:0 0 14px}}
.vers{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}}
.ver{{border:1px solid var(--line);border-radius:10px;padding:12px;background:#11141a}}
.ver.best{{border-color:#30a46c;box-shadow:0 0 0 1px #30a46c inset}}
.vhead{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
.rk{{color:var(--dim);font-size:12px}}
.sc{{font-weight:700;font-size:18px}}
.tag{{background:#30a46c;color:#04120a;border-radius:20px;padding:1px 8px;font-size:11px;font-weight:700}}
.fname{{font-weight:600;word-break:break-all}}
.album{{font-size:12px;color:var(--dim);margin-bottom:8px}}
.dim{{color:#6b7280;font-size:11px}}
.kv{{width:100%;border-collapse:collapse;font-size:12px}}
.kv td{{padding:3px 0;border-bottom:1px solid #1d222c}}
.kv td:first-child{{color:var(--dim);width:78px}}
.bar{{display:inline-block;width:60px;height:6px;background:#232834;border-radius:4px;vertical-align:middle;margin-left:6px}}
.bar i{{display:block;height:100%;border-radius:4px}}
.bdrow{{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}}
.bd{{background:#1b202a;border-radius:6px;padding:2px 7px;font-size:11px;color:var(--dim)}}
.bd b{{color:var(--fg)}}
.flag{{font-size:12px;border-radius:6px;padding:6px 8px;margin-top:6px}}
.flag.bad{{background:#2a1214;color:#ff9da1;border-left:3px solid #e5484d}}
.flag.warn{{background:#2a2210;color:#ffd08a;border-left:3px solid #f5a524}}
footer{{color:#6b7280;font-size:12px;margin-top:24px}}
</style></head><body>
<h1>DupeScope · Same-Track Version Comparison</h1>
<div class="sub">Generated {html.escape(res.get('generated_at',''))} ·
Roots {html.escape(', '.join(st.get('roots', [])))}</div>
<div class="kpis">
  <div class="kpi"><b>{st.get('files_probed',0)}</b><span>Files scanned</span></div>
  <div class="kpi"><b>{st.get('songs_total',0)}</b><span>Tracks found</span></div>
  <div class="kpi"><b>{st.get('songs_multi',0)}</b><span>Multi-version tracks</span></div>
  <div class="kpi"><b>{st.get('files_analyzed',0)}</b><span>Deep analyzed</span></div>
  <div class="kpi"><b>{st.get('songs_with_problems',0)}</b><span>Tracks with issues</span></div>
  <div class="kpi"><b>{st.get('elapsed_sec',0)}s</b><span>Elapsed</span></div>
</div>
{''.join(rows) or '<p>No songs with multiple versions were found.</p>'}
<footer>Cutoff is determined automatically from the FFT power spectrum (aligned with Spek's visual semantics); DR uses the same method as TT DR Meter; 
LUFS/LRA/true peak come from ffmpeg ebur128. Values are for relative comparison only; for borderline material, verify with Spek.</footer>
</body></html>"""
