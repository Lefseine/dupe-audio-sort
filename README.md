# DupeScope · 同曲音质对比 · 挑出最佳版本

> 把散落在多张专辑里的同一首歌找出来，用硬指标排出该留哪版；顺带治好"《同一首歌》按保存时间播放"。
> 纯本地、全免费、不上传任何音频。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 它解决什么

下载了上百 GB 音乐，同一首歌散落在多张 CD 专辑里，文件大小不一。以前靠"选最大的那个"，
学了 HiFi 才知道要看**频谱上限**（Spek）和**动态范围**（JRiver DR）——但手动逐首分析太累。

DupeScope 把这两件事自动化，并补上开源生态里真正有判断力的几个参数：

| 指标 | 识破什么 |
|---|---|
| 频谱上限 (kHz) | 假无损、有损转无损、升频假 Hi-Res |
| 有效位深 | 16→24 位补零升位 |
| DR 动态（TT 口径） | 响度战争型重母带 |
| LUFS / 真峰值 / PLR | 过响、削波风险、母带余量 |
| 削波样本数 | 削顶的烂母带 |
| 声道相关 / 直流偏移 | 假立体声、白占余量的 DC |
| 解码完整性 | 坏块、截断 |
| **AccurateRip 位对齐**（可选） | 抓轨是否与全网逐位一致 |
| **ReplayGain 响度扫描**（可选） | "响的显得好"的比较偏差 |

第二件事：把 `歌名+歌星.wav` 文件夹排出人能预期的顺序——文件名序号、m3u 列表、
WAV 双写标签、时间戳、有序副本五种手段一次给全，每次都有撤销清单。

## 快速开始

```bash
# 装成 WorkBuddy / 兼容 Agent 技能
# 把本仓库文件夹放进 ~/.workbuddy/skills/ 即可

# 先体检（会自动探测本机 ffmpeg/rsgain）
python scripts/launcher.py doctor

# 分析同曲多版本
python scripts/launcher.py analyze "D:\Music"

# 播放顺序方案
python scripts/launcher.py order "D:\Music\同一首歌"

# 网页界面
python scripts/launcher.py serve --port 8765
```

触发词（对 AI 说）："分析这几张专辑的同一首歌"、"哪个版本好"、"播放顺序乱"、
"抓轨对不对"、"响度归一"。

## 设计取舍（为什么值得 star）

- **不调 Spek 出图**：Spek 无 CLI，无法批处理；自己算 FFT，可并发可缓存。
- **不依赖 JRiver**：DR 按 TT 口径本地重算，跨版本可比。
- **位对齐自己实现 AccurateRip v1/v2**：CUETools `/verify` 官方说明跑完不关闭、不能循环，
  做不了批处理；单曲又无 TOC 算不出 discID。所以自动化部分纯 Python 实现，CUETools 退为 GUI 复核。
- **响度归一外挂 rsgain**：只写标签不动音频流，默认在临时副本只读扫描，原目录零改动。
- **技能包仅 87 KB**：ffmpeg（303MB）/ rsgain 不进包，多级探测复用本机已有，找不到给免费下载指引。

## 诚实边界

- 频率上限高 ≠ 好听；DR 低 ≠ 差（老录音本就是高压缩母带）。
- 单曲 WAV/FLAC 没有整盘 TOC，**物理上无法做 AccurateRip 校验**，工具如实提示。
- 解码 0 报错 ≠ 绝对完好；细粒度损坏要靠 `flac -t` / CUETools。

## 依赖

Python 3.10+，`numpy rapidfuzz mutagen pypinyin`，ffmpeg（随本机或按提示下载）。
全部免费开源，无付费服务、无 API Key、无云端上传。

## License

MIT
