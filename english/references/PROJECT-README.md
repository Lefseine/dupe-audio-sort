# DupeScope · Same-Song Identification + Order Correction

Two things, one local web app:

1. **Version identification**: find the same song scattered across multiple albums, automatically compare the hard metrics, and tell you which version to keep.
2. **Order correction**: take a folder like "同一首歌" with "SongName+Artist.wav" files, arrange it in the order a human expects, and write it in a form the player can actually read.
3. **Bit-perfect alignment · Loudness**: use a full-disc cue for AccurateRip bit-for-bit verification (the only way to prove "the rip is correct");
   measure whole-library loudness with rsgain to remove the "louder sounds better" comparison bias.

Entirely local, free, and open source — no need to keep JRiver running; Spek / CUETools are kept for manual visual review (launchable with one click from the UI).

## Quick Start

```
Double-click run.cmd    →  auto-installs dependencies and opens http://127.0.0.1:8765/
```

- Specify a port on the command line: `run.cmd --port 9000`
- Direct scan: `http://127.0.0.1:8765/?auto=D:\Music` (bookmarkable; `&open=0` auto-expands the first group)
- Direct ordering: `http://127.0.0.1:8765/?tab=order&dir=D:\Music\同一首歌`
- Direct bit alignment: `http://127.0.0.1:8765/?tab=tools&dir=D:\Music\专辑目录`
- Right-click menu: `tools\install-menu.cmd` (writes only to HKCU; `remove-menu.cmd` undoes it)

By default, only files with multiple versions of the same song get deep analysis; all other files are metadata-only.
After the first full-library scan, results go into a cache (`.cache/analysis.db`), and subsequent scans only process new or changed files.

## What We Compare

| Metric | Source | What It Exposes |
|---|---|---|
| Spectral cutoff (kHz) | Local FFT (per-band integration + cliff detection) | Fake lossless, lossy-to-lossless transcodes, upsampled fake Hi-Res |
| Effective bit depth | Fixed-point grid back-derived LSB | Undithered 16→24-bit upconversions |
| DR dynamics | TT DR Meter methodology (3 s block crest factor) | Loudness-war-style remastering |
| LUFS / LRA / True Peak / PLR | ffmpeg ebur128 | Over-loudness, clipping distortion risk, mastering headroom |
| Clipped sample count | Per-sample count across the whole track | Clipped, badly mastered audio |
| Narrow-band notch density | 2–16 kHz spectral-floor scan | Lossy-encoding residue fingerprints |
| Channel correlation coefficient | Whole-track ΣLR/√(ΣL²ΣR²) | Mono duplicated to stereo, inverted left/right channels |
| DC offset | Whole-track sample mean | DC introduced by transfer/ripping, wasting dynamic headroom |
| Decode integrity | ffmpeg decode error count | Bad blocks, truncation |

Composite score = spectral authenticity 35 + bit depth/sample rate 20 + dynamics 25 + peak headroom 10 + no clipping 10,
with each item listed separately in the UI — no black-box total score.

> For what else to look at beyond spectrum and dynamics, and which free tools to use, see **[docs/OSS-TOOLCHAIN.md](docs/OSS-TOOLCHAIN.md)**.

## Bit Alignment and Loudness (Third Tab)

### Bit Alignment — A More Fundamental Layer Than Parameters

All the metrics above can only show that something "sounds authentic"; **only bit alignment can prove this rip is bit-for-bit identical to everyone else's online**.
What's implemented is the full AccurateRip v1/v2 pipeline (pure Python, concurrent, cacheable, and able to compute local parts offline):

1. Parse the full-disc `.cue` TOC → compute discID;
2. Decode each track to 16-bit/44.1k PCM and compute the v1 (1-based position-weighted sum) and v2 (64-bit product high-bit fold-back) checksums;
3. Query `www.accuraterip.com`, compare all submissions, and take the highest confidence.

Four result states: `ok` (bit-for-bit identical) / `mismatch` (a track differs — the rip may be faulty or re-encoded) / `not_found` (rare disc absent from the database) / `need_cue` (no full-disc cue).

> **Hard boundary**: AccurateRip computes discID from the full-disc TOC. **Single-track WAV/FLAC files have no TOC and physically cannot be verified** — the tool gives an explicit notice instead of a fake result.
> It also requires 16-bit stereo and drive offset correction applied during ripping — these are requirements of the AccurateRip protocol itself.
> CUETools can be launched with one click for GUI visual review (`/verify` auto-verifies; the official note says it does not close automatically when finished and must be closed manually).

Command line:

```
python -m dsc.accuracy "D:\Music\专辑目录"          # find cue and verify online
python -m dsc.accuracy "D:\Music\专辑目录" --no-net  # compute discID locally only, no network
```

### Loudness Normalization — Eliminating the "Louder Sounds Better" Bias

When comparing versions, the louder one has an inherent advantage. rsgain measures each track's LUFS and suggested gain,
bringing the comparison back to a common loudness baseline. A **bundled official portable build of rsgain 3.8** is included (GPL, no installation, writes tags only and never touches the audio stream).

- **Read-only scan by default**: runs on a temporary copy, leaving the original directory untouched; outputs per-track LUFS / gain / peak.
- **Tags written only after confirmation**: writes standard ReplayGain 2.0 tags that JRiver / foobar / mobile players can apply automatically;
  only tags change, the audio stream does not, and you can undo anytime by rewriting with rsgain.
- Target loudness can be −18 LUFS (ReplayGain standard) or −23 LUFS (EBU R128 broadcast).

Command line:

```
python -m dsc.gain "D:\Music"                 # read-only scan, outputs suggestions
python -m dsc.gain "D:\Music" --apply         # after confirmation, writes ReplayGain tags
```

## Why Playback Order Goes Wrong, and How to Fix It

For folders like `同一首歌/SongName+Artist.wav`, players often play in the order "files were added to the folder", for three reasons:

1. FAT32/exFAT directories are **linked lists appended in write order**, and car/portable players read directory entries directly in that order;
2. WAV has poor tag support, so players cannot read the track number and fall back to sorting by filename;
3. When sorting filenames, Chinese text is compared by UTF-8 bytes, yielding code-point order — neither pinyin nor stroke count.

So the only option is to change "what the player uses to sort". Five methods each target a class of players, and the UI lets you check them all at once:

| Method | Fixes Which Players | Cost |
|---|---|---|
| Prefix filenames with track numbers `01 Song - Artist.wav` | Players that sort by filename/natural order (the vast majority) | Renaming |
| Write `m3u8` (UTF-8 BOM) + `m3u` (GBK) | Playlist-supporting players (car, foobar, JRiver) | Two extra files |
| Write tags: both RIFF INFO + ID3v2 for WAV | Tag-sorting players (JRiver, MusicBee) | File content changed |
| Set timestamps in target order | Players that sort by "date" (some car units) | File times changed |
| Copy into an `_ordered copy` in target order | FAT/exFAT devices that read directory entry order | One extra copy of space |

Safety boundaries: **plan only by default**; files are touched only after clicking Apply and a second confirmation; **files are never deleted**;
name conflicts are skipped and reported, never overwritten; each apply writes an undo manifest to `.dupescope/undo-*.json` for one-click rollback.

The command line works too:

```
python -m dsc.order "D:\Music\同一首歌"                     # view the plan only
python -m dsc.order "D:\Music\同一首歌" --apply --tags      # apply: rename + playlists + tags
python -m dsc.order --undo "D:\Music\同一首歌\.dupescope\undo-20261113-163631.json"
```

## Known Limitations (Stated Honestly)

- **Effective bit depth cannot expose dithered fake 24-bit**: when dithering is added during resampling, the low bits get filled in.
  Such files are caught by spectral cutoff + upsampling detection as a fallback.
- **Low DR ≠ poor sound quality**: some old recordings were highly compressed masters to begin with; low DR is their original character.
- Spectral cutoff readings may differ from Spek's visual reading by ~1 kHz: we take the -50 dB band-level boundary,
  while Spek reads "the last visible bright line" by eye — the methodologies simply differ. The UI offers one-click Spek review.
- DSD (dsf/dff) is converted to 176.4 kHz PCM before analysis; bit-depth detection does not apply to DSD.
- **Channel correlation ≈1 is not necessarily bad**: old mono-era recordings are genuinely identical left and right — that is their original character;
  only "nominally stereo yet completely in phase" is suspicious.
- **Decode error count depends on ffmpeg's reporting granularity**: zero errors does not mean absolutely intact; fine-grained damage needs `flac -t` / CUETools.
- Ordering changes "what the player uses to sort", not the player itself: extremely rigid firmware (order stored in a private database,
  or read by filesystem physical block order) may still ignore it — then only a full replacement with an `_ordered copy` works.

## Architecture (ADR Summary)

- **ADR-1 Bundled decode engine, no driving Spek**: Spek 0.8.2 is GUI-only with no CLI parameters,
  so automation has to compute FFT itself. Benefit: concurrent, cacheable, accurate to 0.01 kHz; cost: readings are not fully identical to Spek (documented).
- **ADR-2 No JRiver dependency, DR recomputed locally**: JRiver stores DR in a private library and MCWS requires it to stay running.
  Recomputing locally with the TT DR methodology serves cross-version comparability; a slot for an MCWS adapter is left open (just add a module under `dsc/`).
- **ADR-3 Two-pass decoding**: main pass streams DR/clipping/bit depth/spectrum, secondary pass runs ebur128; concurrent execution, constant memory.
- **ADR-4 Group first, deep-dive second**: metadata grouping → deep analysis only for multi-version groups. Cost for a 100 GB library drops from O(everything) to O(duplicate tracks).
- **ADR-5 sqlite cache**: keyed by path + size + mtime, making rescans nearly free.
- **ADR-6 Ordering touches only "the sort key", not the player**: player implementations vary wildly; the only stable interfaces are
  these five: filename / tags / playlists / file times / directory entries. Changing all of them hits any player,
  and is far more maintainable than "adapting to one specific player"; cost: files get renamed, hence the undo manifest.
- **ADR-7 Pinyin sorting uses pypinyin, with graceful degradation when missing**: sorting Chinese by code point is meaningless, and a hard dependency hurts portability.
  So pypinyin is optional; when absent it degrades with an explicit UI warning rather than silently giving a wrong order.
- **ADR-8 Bit alignment implements AccurateRip itself, CUETools demoted to GUI review**: CUETools' `/verify` is documented as
  "does not close when finished and cannot be called in a loop", so it cannot silently batch-process a 100 GB library; single-track files also lack a TOC for discID.
  So the automatable parts (TOC→discID→checksum→database lookup→comparison) are implemented in pure Python, concurrent and cacheable;
  CUETools serves only as one-click GUI review. Cost: the methodology must be aligned with AccurateRip ourselves, and it only covers full discs with a cue.
- **ADR-9 Loudness normalization uses external rsgain, read-only by default**: ReplayGain tags must be genuinely readable by JRiver/foobar/mobile players,
  which a self-computed LUFS number cannot provide. rsgain is a free GPL portable build (bundled), writing tags only, never touching the audio stream.
  Scanning runs on a temporary copy by default, leaving the original directory untouched, and writing tags requires a second confirmation — reversibility first.

## Directory Layout

```
run.cmd               double-click entry point
server.py             local server (standard library, no framework)
dsc/ffmpeg_bin.py     external tool location and self-check
dsc/probe.py          metadata (ffprobe)
dsc/analyze.py        acoustic analysis core (spectrum/DR/bit depth/clipping/channels/DC/ebur128)
dsc/group.py          same-song grouping + scoring
dsc/pipeline.py       orchestration (scan/cache/concurrency/progress)
dsc/order.py          playback order correction (sort/rename/tags/playlists/timestamps/undo)
dsc/accuracy.py       bit alignment (cue TOC / AccurateRip v1v2 / database lookup and comparison)
dsc/gain.py           loudness normalization (rsgain read-only scan / write ReplayGain tags)
dsc/report.py         HTML/CSV export
web/index.html        single-page UI (three tabs: version identification / playback order / bit alignment · loudness)
tools/bin/            portable ffmpeg
tools/rsgain/         portable rsgain 3.8 (GPL, tags only)
tools/shellmenu.py    right-click menu installer (HKCU)
docs/OSS-TOOLCHAIN.md free/open-source toolchain research
test/gen_fixtures.py  generates "known-answer" fixtures for regression testing
reports/              HTML/CSV reports from each analysis
```

## Dependencies

Python 3.10+ (run.cmd auto-creates the dependencies), numpy, rapidfuzz, mutagen, pypinyin, ffmpeg (bundled),
rsgain 3.8 (bundled, GPL). Spek / CUETools are optional (used by the manual-review buttons; analysis works without them).
If pypinyin is missing, Chinese falls back to code-point order (the UI issues a warning).
