# Free / Open-Source Toolchain Research: What Else to Look At Beyond Spectrum and Dynamics

Research basis: November 2026; GitHub repository data reflects that day's search results. Star counts are only a rough indicator of "whether anyone uses it", not a quality verdict.
This file answers two things: **which key parameters remain**, and **which free tool is the easiest for each parameter**, plus **why these features were built into DupeScope instead of being called as external tools**.

---

## 1. Summary of Conclusions

1. Spectral cutoff (Spek) and DR dynamics (JRiver / TT DR) are only **two of six trustworthy metrics**. The other four carry no less information: **effective bit depth / True Peak and PLR / clipped samples / decode integrity**.
2. The most decisive veto metric is not sound quality but **bit alignment**: AccurateRip / CTDB verification tells you "is this rip bit-for-bit identical to thousands of others online?". It lives in no player — only in CUETools / EAC — and requires a full-disc cue.
3. Ready-made "fake lossless detection" projects on GitHub are almost all **visualization tools** (Spek, Friture), not **batch decision engines**. What's missing is not algorithms but an orchestration layer that "runs through 100 GB and produces comparable conclusions" — exactly the gap DupeScope fills.
4. The playback order problem (同一首歌 playing by write time) is **unsolvable at the player level** in software; you can only change "what the player uses to sort": filename track numbers, tag track numbers, m3u playlists, timestamps, FAT directory entry order. Five methods map to five classes of player implementations; see the ordering tab notes in `web/index.html`.

---

## 2. Parameter × Tool Master Table

| # | Parameter | What It Answers | Free Tool | Batch / Automatable | DupeScope |
|---|---|---|---|---|---|
| 1 | Spectral cutoff (kHz) | Low-pass filtered? Fake lossless? | **Spek** (GUI), **Friture**, ffmpeg `showspectrumpic` | Spek ❌ (no CLI); Friture ❌; ffmpeg ✅ | ✅ Self-computed 1/12-octave integration + cliff detection |
| 2 | Effective bit depth | 16→24-bit upconversion? | No mature free tool (AuCDtect is closed-source and abandoned) | — | ✅ Fixed-point grid back-derived LSB |
| 3 | DR dynamics | How compressed is the master? | **dr14_t.meter** (Python), foobar2000 DR plugin, JRiver | dr14 ✅ CLI; foobar semi-automatic | ✅ TT DR methodology recomputed |
| 4 | LUFS-I / LRA | Overall loudness, loudness range | **ffmpeg ebur128**, **rsgain**, **loudgain** | ✅ All CLI | ✅ Via ebur128 |
| 5 | True Peak / PLR | Intersample overs, mastering headroom | ffmpeg ebur128 `peak=true`, rsgain | ✅ | ✅ Also computes PLR |
| 6 | Clipped sample count | How much the waveform is flattened | ffmpeg `astats`, Audacity | astats ✅ (scattered readouts) | ✅ Per-sample count |
| 7 | Narrow-band notch | Lossy-encoding residue fingerprint | ffmpeg spectrogram (by eye) | ❌ | ✅ 2–16 kHz spectral-floor scan |
| 8 | Decode integrity | Bad blocks / truncation? | **flac -t**, **shntool**, ffmpeg | ✅ | ✅ Counts decode errors along the way |
| 9 | Channel correlation / fake stereo | Mono duplicated to stereo? Inverted phase? | ffmpeg `aphasemeter`, Audacity | Semi-automatic | ✅ Whole-track correlation coefficient |
| 10 | DC offset | Wasted dynamic headroom | Audacity, ffmpeg `astats` | Semi-automatic | ✅ Whole-track DC |
| 11 | Bit alignment verification | Is the rip identical to others online? | **CUETools / CUERipper**, **Exact Audio Copy** | ✅ (needs full-disc cue + network) | ✅ Built-in AccurateRip v1/v2 as of 2026-11 (see §5) |
| 12 | Recording identity | Are two versions the same recording? | **Chromaprint / fpcalc** + AcoustID, **beets** `duplicates` plugin | ✅ | ⚠️ Clusters by title + duration + artist + fast hash; AcoustID needs network + third-party key, see §7 |

---

## 3. Reviews by Use Case (with GitHub Measured Data)

### 3.1 Spectrum / Visualization
- **Spek** (spek.cc): the de facto standard, GPL, **GUI-only with no CLI parameters**. Good for manual review of single tracks, not for batch processing.
- **Friture** (`tlecomte/friture`, ~1.1k★, Python): real-time spectrum/waterfall display, useful for teaching and troubleshooting device chains, but not a decision engine.
- **ffmpeg `showspectrumpic`**: can output PNG but far less readable than Spek; good for report illustrations, not for interpretation.

### 3.2 Dynamics / Loudness
- **dr14_t.meter** (`simon-r/dr14_t.meter`, ~139★, Python): implements the Pleasurize Music Foundation DR14 procedure, the open-source option closest to the "official methodology". CLI, batch-capable, few dependencies. **If you don't want to use this tool, this is the top alternative.**
- **rsgain** (`complexlogic/rsgain`, ~732★, C++): batch ReplayGain 2.0 (EBU R128) tagging, extremely fast, with prebuilt Windows binaries. It measures loudness rather than dynamics, but `-s` can also output peak/loudness, making it well suited for whole-library "loudness normalization".
- **loudgain** (`ghedo/loudgain`, ~20★, C): long-standing EBU R128 normalizer, single-purpose but solid.
- **foobar2000 + DR Meter plugin**: the easiest batch DR on Windows; downside is that results live in the UI and you have to figure out exporting yourself.

### 3.3 Library Management and Same-Song Grouping
- **beets** (`beetbox/beets`, ~15.9k★, MIT): the `duplicates` plugin supports `-m` to decide "is this the same audio" by chroma fingerprint, more reliable than filenames; the `chroma` plugin connects to AcoustID. **This is the alternative for parameter #12, which this tool does not implement**, at the cost of building a library, requiring network access, and poor handling of Chinese filenames.
- **MusicBrainz Picard**: fingerprinting + tag completion, good for turning bare filenames like "SongName+Artist.wav" into proper metadata.

### 3.4 Ripping and Bit Alignment
- **CUETools / CUERipper** (by `gchudov`, with `db.cue.tools` as its DB server, Go): dual-database verification with AccurateRip + CTDB, able to conclude "identical to N other people's rips" and repair offsets. **This is the only way to prove "the file itself is correct"**; all other metrics only show "it sounds authentic".
- **Exact Audio Copy**: the gold standard for ripping, with AccurateRip built in; free but closed-source.
- **ARCUE** (`mpe85/arcue-docker`): a Dockerized version of CUETools, suited to scripted verification; on Windows the native build is easier.
- **shntool / flac -t**: lossless integrity checks — one command validates a whole collection; worth running regularly.

### 3.5 Tags / Naming / Order
- **mutagen** (Python, GPL-2.0): the de facto standard library for WAV/FLAC/MP3 tag reading and writing; DupeScope's tagging is built on it.
- **pypinyin** (`mozillazg/python-pinyin`, ~5.4k★, MIT): converts Chinese characters to pinyin, a prerequisite for sorting Chinese by pinyin. **Without it, "alphabetical sort" for Chinese is just code-point order — a meaningless result.**
- **Mp3tag / MusicBee / foobar2000**: free and closed-source, with mature batch renaming and tagging; but none of them solve the "FAT directory order" layer.
- Playlist formats: `m3u8` (UTF-8) + `m3u` (local code page). Older car units only recognize the latter, so both are written.

### 3.6 Fake Lossless Detection (Free but Closed-Source, for Reference)
- **Lossless Audio Checker**: free closed-source for Windows, judges by spectrum + MPEG residue, with three classes: "suspicious / upsampled / transcoded". Direct conclusions, but a black box with no batch scripting.
- **AuCDtect-Task / Tau Analyzer**: long-standing tools, abandoned for years, with rising false-positive rates on modern encoders — not recommended as the sole basis.
- **Fakin' the Funk / Musicscope**: commercial software, out of scope here.

---

## 4. Two Tools Still Worth Installing (Covering DupeScope's Blind Spots)

| Tool | What It Adds | When to Use |
|---|---|---|
| **CUETools** | Bit alignment (AccurateRip/CTDB) | When you get a freshly ripped disc, verify it first; this is the only method that can give an "absolutely correct" verdict |
| **rsgain** | Whole-library loudness normalization + ReplayGain tags | After tidying your library, to compare tracks from different masters at the same volume instead of "louder sounds better" |

Picard is optional: if you want these old records to also have correct artist, year, and cover art in JRiver / on your phone.

---

## 5. Why These Features Are "Built In" Rather Than "Called Externally"

| Approach | Benefit | Cost | Verdict |
|---|---|---|---|
| Drive Spek to render an image, then read the image | Exactly matches Spek's readings | Spek has no CLI; image reading is unreliable; cannot run concurrently | ❌ Rejected |
| Read DR from JRiver's library | Matches JRiver's display | Requires JRiver to stay running, private library format, no batch export | ❌ Rejected (MCWS adapter slot left open) |
| Delegate everything to beets | Mature ecosystem | Must build a library, requires network, poor Chinese filename handling, first import of 100 GB takes ages | ❌ Reference only |
| Self-compute + cache | Concurrent, cacheable, precise values, offline | Methodology must be defined and documented ourselves (already written in the README's "Known Limitations") | ✅ Adopted |
| Bit alignment via CUETools GUI only | Matches official readings | Documented as not closing after a run and not loopable, so no batch processing; single tracks have no TOC | ❌ Changed to implementing AccurateRip ourselves (ADR-8) |
| Loudness normalization that only reports a LUFS number | Zero dependencies | A number cannot take effect across players; standard ReplayGain tags are needed | ❌ External bundled rsgain (ADR-9) |

Corresponding architecture decisions: ADR-1 (self-computed spectrum), ADR-2 (local DR), ADR-3 (two-pass decoding), ADR-4 (group first, deep-dive second), ADR-5 (sqlite cache),
ADR-8 (self-computed AccurateRip), ADR-9 (external rsgain, read-only by default).
The added items 9/10 (channel correlation, DC) are **computed along the way**: whole-track samples are already available in the same decoding pass, so just five dot products are needed — marginal cost ≈ 0.

---

## 7. Why Item 12 "Recording Identity" Was Not Implemented, and the Alternative

Chromaprint/fpcalc + AcoustID can answer "are these two versions the same recording" (more reliable than filenames), but:
AcoustID requires network access and an application key, and the fingerprint database's metadata coverage for Chinese tracks is uneven —
this conflicts with the project's "fully local, zero registration, zero keys" constraint. The ffmpeg essentials build also lacks the chromaprint filter.

Current alternative: `dsc/group.py` clusters by **title normalization + artist similarity + duration tolerance + a blake2b fast hash of the first 1 MB of the file**,
which already covers the practical scenario of "multiple versions of the same song"; identical fast hashes are marked directly as `duplicate_of`.
Cost: the same recording with edited tags or different file headers can be treated as two versions — but this only happens with "same recording, different container",
and does not affect the "which version to keep" conclusion (the metrics get compared anyway).

If professional-grade identity detection is needed later: just install `beets` + `chromaprint` for offline comparison; this tool does not inline it.

---

## 6. References

- Spek — https://spek.cc
- dr14_t.meter — https://github.com/simon-r/dr14_t.meter
- rsgain — https://github.com/complexlogic/rsgain
- loudgain — https://github.com/ghedo/loudgain
- beets — https://github.com/beetbox/beets
- pypinyin — https://github.com/mozillazg/python-pinyin
- mutagen — https://mutagen.readthedocs.io
- CUETools DB — https://github.com/gchudov/db.cue.tools
- Friture — https://github.com/tlecomte/friture
- ffmpeg filters (ebur128 / astats / showspectrumpic) — https://ffmpeg.org/ffmpeg-filters.html
