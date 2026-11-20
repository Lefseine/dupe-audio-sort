---
name: dupe-audio-sort-user-ac60b2be
slug: dupe-audio-sort-user-ac60b2be
displayName: 同曲音质对比 · 挑出最佳版本
displayName_en: Compare Same-Track Versions, Pick the Best
version: 1.3.0
summary: 同一首歌散落在多张专辑？自动对比频谱上限、位深、动态范围与削波，告诉你该留哪一版；并修正多版本文件夹的播放顺序。纯本地、免费、不上传音频。
description: |
  把散落在多张专辑里的同一首歌找出来，用硬指标（频谱上限、有效位深、DR 动态、真峰值、削波、声道相关、DC 偏移）排出该留哪版；
  修正《同一首歌》文件夹按写入时间播放的乱序；可选做 AccurateRip 位对齐与 rsgain 响度归一。全部在本机运行，音频不外传。
description_zh: 分析本地音乐库，排出同一首歌各版本高下、修正播放顺序、可选位对齐与响度扫描。纯本地免费。
description_en: Rank multiple versions of the same song locally by spectrum, bit depth, dynamic range and clipping; fix playback order; optional AccurateRip and ReplayGain. Fully local and free.
license: MIT
author: user-ac60b2be
tags:
  - 音频
  - 音乐
  - HiFi
  - 音质分析
  - 无损检测
  - 本地工具
homepage: https://github.com/Lefseine/dupe-audio-sort
examples_zh:
  - 分析 D:\Music 里《恰似你的温柔》各版本，告诉我留哪个
  - 把《同一首歌》文件夹按歌星拼音排序并生成播放列表
  - 检查这几张 CD 抓轨有没有报错（位对齐）
  - 首页选「跨专辑挑同名歌」，从蔡琴的多张专辑里挑《恰似你的温柔》对比
  - 首页选「同一张专辑多来源择优」，整盘逐首比较哪个下载源更好
examples_en:
  - Analyze all versions of a song in my music folder and tell me which to keep
  - Sort my same-song folder by artist name and generate a playlist
---


# 同曲甄别 · 音频版本管家（DupeScope 技能封装）

把散落在多张专辑里的同一首歌找出来，用硬指标排出高下；顺带解决"《同一首歌》文件夹播放顺序乱"。
**全部在本机运行，音频不外传，不依赖任何付费服务。**

## 何时触发

用户给出音乐文件路径并表达以下任一意图时，用本技能：

- 比较/挑选同一首歌的多个版本（"哪个音质好""该删哪版""假无损"）
- 查音质指标（频谱上限、位深、动态 DR、LUFS、削波、声道、DC 偏移）
- 播放顺序（"按歌星排""车载机按保存时间播""WAV 没顺序"）
- 位对齐（"抓轨对不对""AccurateRip""cue 校验"）
- 响度（"响度归一""ReplayGain""响的显得好"）

## 运行环境

统一入口：`scripts/launcher.py`。**用当前会话的 Python 解释器调用它**（AI 用哪个 python，就传哪个）：

```
python <skill>/scripts/launcher.py <子命令> [参数]
```

其中 `<skill>` 是本技能目录（本文件所在目录）。入口会自动：
1. 把 `scripts/engine/`（随包引擎，纯 Python）加入模块路径；
2. 探测本机 ffmpeg / ffprobe / rsgain（技能的 `~/.dupe-audio-data/tools` → `~/WorkBuddy/*/dupescope/tools` → PATH），探测到就注入引擎，无需重复下载 300 MB 二进制；
3. 把缓存和报告写到 `~/.dupe-audio-data/`，不写脏技能目录，升级技能不丢数据。

**首次使用先体检**：

```
python <skill>/scripts/launcher.py doctor
```

返回 JSON 里 `ready=true` 才能继续。若 `ready=false`，按 `hints` 数组逐条处理：
缺 Python 依赖就 `pip install numpy rapidfuzz mutagen pypinyin`；缺 ffmpeg 就按提示下载免费的
essentials build 放到 `~/.dupe-audio-data/tools/bin/`。**这些都是免费开源软件，不要推荐付费替代品。**

## 子命令决策表

| 用户意图 | 子命令 | 说明 |
|---|---|---|
| 体检 / 首次使用 | `doctor` | 查依赖与工具，返回 JSON |
| 比较同曲多版本 | `analyze <目录> [目录...]` | 分组 + 深度分析，输出排名 JSON 与报告路径 |
| 播放顺序乱 | `order <目录>` | 只出拼音排序方案，**默认不改动文件** |
| 抓轨对不对 | `align <目录> [--offline]` | 找 cue 做 AccurateRip 校验，无 cue 会说 need_cue |
| 响度是否公平 | `gain <目录>` | **只读扫描**，在临时副本上跑，原目录零改动 |
| 想用网页界面 | `serve [--port 8765]` | 起本地服务并开浏览器（默认端口 8765） |

### analyze —— 版本甄别（核心）

用户说"分析/比较/挑版本"时跑：

```
python <skill>/scripts/launcher.py analyze "D:\Music"
```

输出 JSON：`stats`（文件数/多版本曲目数）、`groups`（每组含 rank/score/cutoff_khz/dr/flags/advice）、
`report_html`/`report_csv`。**读 JSON 后用自然语言回答用户**：哪版最好、依据是什么（引用 cutoff/DR/位深）、
哪版该淘汰、有没有需要警惕的 flag。不要只丢 JSON 给用户。

指标口径与边界见 `references/PROJECT-README.md` 和 `references/OSS-TOOLCHAIN.md`。
关键诚实边界：频率上限高不等于好听；DR 低不等于差（老录音本就是高压缩母带）；
单声道老录音的"声道相关≈1"是正常的，不是缺陷。

### order —— 播放顺序

```
python <skill>/scripts/launcher.py order "D:\Music\同一首歌"
```

输出按拼音排好的方案（歌名→歌星）。**本子命令只出方案、不改文件**。
用户确认要落地时，告诉用户：网页界面（`serve`）里有"应用"按钮，含改名 / m3u 列表 /
WAV 标签 / 时间戳 / 有序副本五种手段，且每次都有撤销清单；**不要用命令行直接改**，
因为在网页里能先看后改、还能一键撤销。

### align —— 位对齐

```
python <skill>/scripts/launcher.py align "D:\Music\专辑目录"
python <skill>/scripts/launcher.py align "D:\Music\专辑目录" --offline   # 只算本地，不联网
```

返回 `status`：`ok`（逐位一致）/ `mismatch`（有轨不一致）/ `not_found`（库里没这张碟）/
`need_cue`（没有整盘 cue）。**必须如实转述 `need_cue`**：单曲 WAV/FLAC 没有 TOC，
物理上无法做 AccurateRip 校验，不要含糊过去。CUETools GUI 复核是可选的，没装不影响本功能。

### gain —— 响度归一

```
python <skill>/scripts/launcher.py gain "D:\Music"
```

只读扫描，输出每条曲目的 LUFS、建议增益、峰值。默认目标 -18 LUFS（ReplayGain 标准）。
**only scan, never write by default**：写 ReplayGain 标签要到网页界面点"确认写入"，
且那也只改标签不动音频流、可重写撤销。回答用户时解释清楚"响的显得好"这一比较偏差。

### serve —— 网页界面

```
python <skill>/scripts/launcher.py serve --port 8765
```

会起服务并尝试开浏览器。如果用户机器 8765 已被占用，换端口（如 8790）。
关掉运行窗口即停止。

## 边界与禁止事项

- **不上传任何音频**，不调用任何云端音频分析服务——用户的上百 GB 音乐必须留在本机。
- **不推荐付费软件**。ffmpeg / rsgain / CUETools / Spek 全部免费；Spek 与 CUETools 只是 GUI 复核，非必需。
- **默认只读**。analyze / gain / order 都不改文件；任何改动都要用户明确确认，并优先走有撤销清单的网页界面。
- **不碰版权判断**，只做技术参数分析。
- AccurateRip 需要的是"整盘 cue + 已做过驱动器偏移校正的 16-bit 立体声抓轨"，这是协议本身要求，
  不满足时如实说 `need_cue`，不要用其它指标冒充位对齐结论。

## 详细参考

- `references/PROJECT-README.md` — 指标全表、架构决策、已知边界、命令行用法
- `references/OSS-TOOLCHAIN.md` — 免费开源工具链调研、每个参数对应哪个工具
