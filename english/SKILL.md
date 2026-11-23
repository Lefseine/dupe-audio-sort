---
name: dupe-audio-sort-en
slug: dupe-audio-sort-en
displayName: Audio Version Finder · Pick the Best Copy
displayName_en: Audio Version Finder · Pick the Best Copy
display_name: 音频版本甄别 · 挑出最佳版本（英文版）
display_name_en: Audio Version Finder · Pick the Best Copy
version: 1.4.0
summary: The same song scattered across albums? Automatically compare spectral cutoff, effective bit depth, dynamic range and clipping to tell you which copy to keep; also fixes playback order. Fully local, free, never uploads audio.
description: |
  Find multiple versions of the same song scattered across albums and rank them with hard metrics (spectral cutoff,
  effective bit depth, DR dynamic range, true peak, clipping, channel correlation, DC offset) so you know which to keep.
  Fixes the "same song" folder that plays in file-save order instead of by title/artist. Optional AccurateRip bit-perfect
  verification and rsgain loudness scanning. Everything runs on your machine; audio is never uploaded.
description_zh: 英文版 · 本地对同一首歌的多个版本排序（频谱、位深、动态、削波），修正播放顺序，可选 AccurateRip 位对齐与 ReplayGain 响度扫描。纯本地、免费、不上传音频。
description_en: |
  Rank multiple versions of the same song locally by spectral cutoff, bit depth, dynamic range and clipping;
  fix playback order; optional AccurateRip verification and ReplayGain scanning. Fully local and free.
license: MIT
author: user-ac60b2be
homepage: https://github.com/Lefseine/dupe-audio-sort
tags:
  - audio
  - music
  - hifi
  - audio-quality
  - lossless-detection
  - local-tool
examples_en:
  - Analyze all versions of a song in D:\Music and tell me which to keep
  - Sort my "Same Song" folder by artist name and generate a playlist
  - Check whether these CD rips bit-match the AccurateRip database
  - Scan my library's loudness and show me which versions win by being louder
examples_zh:
  - 分析 D:\Music 里同一首歌的各版本，告诉我留哪个
  - 把《同一首歌》文件夹按歌星拼音排序并生成播放列表
  - 检查这几张 CD 抓轨有没有报错
category: productivity
platforms: [WorkBuddy, Claude Code, Cursor]
allowed-tools: Bash,Read,Write
---

# Audio Version Finder (DupeScope · English Edition)

Find every copy of the same song scattered across albums and rank them with hard metrics — then simply tell the user which one to keep and why. Also solves the "my Same-Song folder plays in file-save order" problem.
**Everything runs on the local machine. Audio is never uploaded, and no paid service is required.**

## When to use this skill

Use it when the user gives a music folder path and expresses any of these intents:

- Compare / pick among multiple versions of one song ("which sounds better", "which to delete", "fake lossless")
- Check audio-quality metrics (spectral cutoff, bit depth, dynamic DR, LUFS, clipping, channel correlation, DC offset)
- Playback order ("sort by artist", "car stereo plays in save order", "WAV has no order")
- Bit-perfect verification ("is my rip correct", "AccurateRip", "cue verification")
- Loudness ("normalize loudness", "ReplayGain", "the louder one seems better")

## Runtime

Unified entry point: `scripts/launcher.py`. **Call it with the current session's Python interpreter** (whichever Python the AI uses, pass that one):

```
python <skill>/scripts/launcher.py <subcommand> [args]
```

Here `<skill>` is this skill's directory (where this file lives). The entry point automatically:

1. Adds `scripts/engine/` (the bundled pure-Python engine) to the module path;
2. Detects local ffmpeg / ffprobe / rsgain (search order: the skill's `~/.dupe-audio-data/tools` → `~/WorkBuddy/*/dupescope/tools` → PATH), injecting whatever it finds so there is no need to re-download ~300 MB of binaries;
3. Writes cache and reports to `~/.dupe-audio-data/`, never into the skill directory, so upgrades never lose data.

**Run a doctor check first on first use**:

```
python <skill>/scripts/launcher.py doctor
```

Only continue when the returned JSON has `ready=true`. If `ready=false`, work through the `hints` array: for missing Python dependencies run `pip install numpy rapidfuzz mutagen pypinyin`; for missing ffmpeg follow the hint to download the free essentials build into `~/.dupe-audio-data/tools/bin/`. **These are all free and open source — never recommend paid alternatives.**

## Subcommand decision table

| User intent | Subcommand | Notes |
|---|---|---|
| Health check / first use | `doctor` | Checks dependencies and tools, returns JSON |
| Compare versions of one song | `analyze <dir> [dir...]` | Groups + deep analysis, outputs ranking JSON and report paths |
| Playback order wrong | `order <dir>` | Produces a pinyin-sorted plan only; **does not modify files by default** |
| Is my rip correct | `align <dir> [--offline]` | Finds cue and runs AccurateRip; reports need_cue if none |
| Is loudness fair | `gain <dir>` | **Read-only scan** on a temp copy; original folder untouched |
| Wants a web UI | `serve [--port 8765]` | Starts a local server and opens the browser (default port 8765) |

### analyze — version comparison (core)

Run when the user says "analyze / compare / pick a version":

```
python <skill>/scripts/launcher.py analyze "D:\Music"
```

Output JSON: `stats` (file count / multi-version song count), `groups` (each with rank/score/cutoff_khz/dr/flags/advice),
`report_html` / `report_csv`. **After reading the JSON, answer the user in natural language**: which version is best, on what
evidence (cite cutoff / DR / bit depth), which to drop, and any flag to be wary of. Never just dump the JSON on the user.

Metric definitions and boundaries are in `references/PROJECT-README.md` and `references/OSS-TOOLCHAIN.md`.
Key honest boundaries: a higher frequency ceiling does not mean "sounds better"; low DR does not mean bad (old recordings
were often mastered highly compressed); "channel correlation ≈ 1" on an original mono recording is normal, not a defect.

### order — playback order

```
python <skill>/scripts/launcher.py order "D:\Music\Same Song"
```

Outputs a pinyin-sorted plan (title → artist). **This subcommand only produces a plan; it does not modify files.**
When the user confirms they want to apply it, tell them: the web UI (`serve`) has an Apply button with five methods —
rename / m3u playlist / WAV tags / timestamps / ordered copies — and every action writes an undo manifest; **do not
modify files directly from the command line**, because the web UI lets you preview before changing and undo in one click.

### align — bit-perfect verification

```
python <skill>/scripts/launcher.py align "D:\Music\album"
python <skill>/scripts/launcher.py align "D:\Music\album" --offline   # local only, no network
```

Returns `status`: `ok` (bit-for-bit match) / `mismatch` (a track differs) / `not_found` (disc not in database) /
`need_cue` (no full-disc cue). **Always relay `need_cue` honestly**: a single WAV/FLAC has no TOC, so AccurateRip
verification is physically impossible — do not gloss over this. The optional CUETools GUI review is not required.

### gain — loudness normalization

```
python <skill>/scripts/launcher.py gain "D:\Music"
```

Read-only scan; outputs each track's LUFS, suggested gain and peak. Default target is -18 LUFS (ReplayGain standard).
**Scan only — never write by default**: writing ReplayGain tags requires clicking Confirm in the web UI, and even then
only tags change (not the audio stream), which is reversible. When answering, explain the "louder seems better" bias.

### serve — web interface

```
python <skill>/scripts/launcher.py serve --port 8765
```

Starts the server and tries to open a browser. If port 8765 is taken on the user's machine, use another port (e.g. 8790).
Close the running window to stop.

## Boundaries and prohibitions

- **Never upload any audio**, never call a cloud audio-analysis service — the user's hundreds of GB of music must stay local.
- **Never recommend paid software.** ffmpeg / rsgain / CUETools / Spek are all free; Spek and CUETools are GUI-only review aids, not required.
- **Read-only by default.** analyze / gain / order never modify files; any change needs explicit user confirmation and should go through the web UI, which keeps an undo manifest.
- **No copyright judgments**, technical parameter analysis only.
- AccurateRip requires "a full-disc cue + a 16-bit stereo rip with drive-offset correction" — that is the protocol's own requirement. When unmet, say `need_cue` honestly and never substitute another metric for a bit-perfect conclusion.

## Detailed references

- `references/PROJECT-README.md` — full metric table, architecture decisions, known boundaries, CLI usage
- `references/OSS-TOOLCHAIN.md` — free open-source toolchain research, which tool covers each parameter
