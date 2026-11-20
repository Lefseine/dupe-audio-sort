# 免费 / 开源工具链调研：频谱与动态之外，还该看什么

调研口径：2026-11，GitHub 仓库数据为当日检索结果；星数只作"有没有人用"的粗略参考，不作质量结论。
本文件回答两件事：**还有哪些关键参数**、**每个参数用哪个免费工具最省事**，以及**为什么这些功能被做进了 DupeScope 而不是外挂调用**。

---

## 1. 结论摘要

1. 频谱上限（Spek）与 DR 动态（JRiver / TT DR）只是**六个可信指标里的两个**。剩下四个的信息量不比它们小：**有效位深 / 真峰值与 PLR / 削波样本 / 解码完整性**。
2. 最能一票否决的指标不是音质，而是**位对齐**：AccurateRip / CTDB 校验告诉你"这份抓轨和全网几千份逐位一致吗"。它不在任何播放器里，只在 CUETools / EAC 里，且需要整盘 cue。
3. GitHub 上现成的"假无损检测"项目几乎都是**可视化工具**（Spek、Friture）而不是**批量判定器**。缺的不是算法，是"把 100 GB 走一遍并给出可比较结论"的编排层——这正是 DupeScope 补的位置。
4. 播放顺序问题（《同一首歌》按写入时间播）在软件层面**无解于播放器**，只能改"播放器用来排序的东西"：文件名序号、标签 track、m3u 列表、时间戳、FAT 目录条目顺序。五种手段对应五类播放器实现，见 `web/index.html` 里的排序页说明。

---

## 2. 参数 × 工具 总表

| # | 参数 | 它回答什么 | 免费工具 | 可批量 / 可自动化 | DupeScope |
|---|---|---|---|---|---|
| 1 | 频谱截止 (kHz) | 有没有被低通？假无损？ | **Spek**（GUI）、**Friture**、ffmpeg `showspectrumpic` | Spek ❌（无 CLI）；Friture ❌；ffmpeg ✅ | ✅ 自算 1/12 倍频程积分 + 断崖检测 |
| 2 | 有效位深 | 16→24 位升位？ | 无成熟免费工具（AuCDtect 闭源且已停更） | — | ✅ 定点网格反推 LSB |
| 3 | DR 动态 | 母带压缩多重？ | **dr14_t.meter**（python）、foobar2000 DR 插件、JRiver | dr14 ✅ 命令行；foobar 半自动 | ✅ TT DR 同口径重算 |
| 4 | LUFS-I / LRA | 整体响度、响度区间 | **ffmpeg ebur128**、**rsgain**、**loudgain** | ✅ 全部命令行 | ✅ 走 ebur128 |
| 5 | 真峰值 / PLR | 采样间过冲、母带余量 | ffmpeg ebur128 `peak=true`、rsgain | ✅ | ✅ 并算 PLR |
| 6 | 削波样本数 | 波形被削平多少 | ffmpeg `astats`、Audacity | astats ✅（读数分散） | ✅ 逐样本计数 |
| 7 | 窄带缺口 | 有损编码残留指纹 | ffmpeg 频谱图（肉眼） | ❌ | ✅ 2–16 kHz 谱底扫描 |
| 8 | 解码完整性 | 文件有没有坏块/截断 | **flac -t**、**shntool**、ffmpeg | ✅ | ✅ 顺带统计解码报错条数 |
| 9 | 声道相关 / 假立体声 | 单声道复制成双声道？反相？ | ffmpeg `aphasemeter`、Audacity | 半自动 | ✅ 整轨相关系数 |
| 10 | 直流偏移 | 白占动态余量 | Audacity、ffmpeg `astats` | 半自动 | ✅ 整轨 DC |
| 11 | 位对齐校验 | 抓轨和全网一致吗 | **CUETools / CUERipper**、**Exact Audio Copy** | ✅（需整盘 cue + 联网） | ✅ 2026-11 已内建 AccurateRip v1/v2（见 §5） |
| 12 | 录音同一性 | 两版是不是同一录音 | **Chromaprint / fpcalc** + AcoustID、**beets** `duplicates` 插件 | ✅ | ⚠️ 用歌名+时长+艺人+快速哈希聚类；AcoustID 需联网+第三方 key，见 §7 |

---

## 3. 按用途点评（含 GitHub 实测数据）

### 3.1 频谱 / 可视化
- **Spek**（spek.cc）：事实标准，GPL，**纯 GUI、无命令行参数**。适合人工复核单曲，不适合批处理。
- **Friture**（`tlecomte/friture`，约 1.1k★，Python）：实时频谱/瀑布图，教学与排查设备链好用，不是判定器。
- **ffmpeg `showspectrumpic`**：能出 PNG 但阅读体验远不如 Spek；适合做报告配图，不适合判读。

### 3.2 动态 / 响度
- **dr14_t.meter**（`simon-r/dr14_t.meter`，约 139★，Python）：按 Pleasurize Music Foundation 的 DR14 流程实现，最接近"官方口径"的开源品。命令行、可批量、依赖少。**如果不想用本工具，这是首选替代。**
- **rsgain**（`complexlogic/rsgain`，约 732★，C++）：ReplayGain 2.0（EBU R128）批量打标签，速度极快，Windows 有预编译包。算的是响度不是动态，但 `-s` 能顺带出 peak/响度，做整库"响度归一"很合适。
- **loudgain**（`ghedo/loudgain`，约 20★，C）：老牌 EBU R128 归一化，功能单一但稳。
- **foobar2000 + DR Meter 插件**：Windows 上最省事的批量 DR，缺点是结果在 UI 里，导出要自己想办法。

### 3.3 库管理与同曲归并
- **beets**（`beetbox/beets`，约 15.9k★，MIT）：`duplicates` 插件支持 `-m` 按 chroma 指纹判定"是不是同一段音频"，比文件名可靠；`chroma` 插件接 AcoustID。**这是本工具第 12 项参数没做的替代方案**，代价是要建库、要联网、对中文命名不友好。
- **MusicBrainz Picard**：指纹识别 + 补标签，适合把"歌名+歌星.wav"这类裸文件名补成规范元数据。

### 3.4 抓轨与位对齐
- **CUETools / CUERipper**（作者 `gchudov`，`db.cue.tools` 为其 DB 服务端，Go）：AccurateRip + CTDB 双库校验，能给出"与 N 个其他人的抓轨一致"的结论，也能修复偏移。**这是唯一能证明"这份文件本身是对的"的手段**，其余所有指标都只是"听起来像真的"。
- **Exact Audio Copy**：抓轨黄金标准，内置 AccurateRip；免费闭源。
- **ARCUE**（`mpe85/arcue-docker`）：CUETools 的 Docker 化版本，适合脚本化校验，Windows 上不如原生省事。
- **shntool / flac -t**：无损完整性校验，一条命令验完整柜子，建议定期跑。

### 3.5 标签 / 命名 / 顺序
- **mutagen**（Python，GPL-2.0）：WAV/FLAC/MP3 标签读写的事实标准库，DupeScope 的标签能力基于它。
- **pypinyin**（`mozillazg/python-pinyin`，约 5.4k★，MIT）：汉字转拼音，中文按拼音排序的前提。**没有它，"字母排序"对中文就是码位序，结果毫无意义。**
- **Mp3tag / MusicBee / foobar2000**：免费闭源，批量改名与标签很成熟；但都不解决"FAT 目录顺序"这一层。
- 播放列表格式：`m3u8`（UTF-8）+ `m3u`（本地代码页）。老车载只认后者，所以两份都写。

### 3.6 真假无损判定（闭源免费，供参考）
- **Lossless Audio Checker**：Windows 免费闭源，用频谱 + MPEG 残留判定，"可疑/升频/转码"三分类。结论直接，但黑箱、不支持批量脚本。
- **AuCDtect-Task / Tau Analyzer**：老牌，停更多年，对现代编码器误判率上升，不建议作唯一依据。
- **Fakin' the Funk / Musicscope**：商业软件，不在本方案内。

---

## 4. 仍然建议你装的两个（补 DupeScope 的盲区）

| 工具 | 补什么 | 什么时候用 |
|---|---|---|
| **CUETools** | 位对齐（AccurateRip/CTDB） | 拿到一张新抓的碟，先验它；这是唯一能给出"绝对正确"的手段 |
| **rsgain** | 整库响度归一 + ReplayGain 标签 | 库整理完之后，让不同母带的曲目在同一音量下比较，而不是"响的显得好" |

Picard 可选：如果你想让这些老唱片在 JRiver / 手机上也有正确的艺人、年份、封面。

---

## 5. 为什么这些功能是"内建"而不是"外挂调用"

| 方案 | 收益 | 代价 | 结论 |
|---|---|---|---|
| 驱动 Spek 出图再识图 | 与 Spek 读数完全一致 | Spek 无 CLI；识图不可靠；无法并发 | ❌ 否决 |
| 读 JRiver 库里的 DR | 与 JRiver 显示一致 | 必须 JRiver 常驻、私有库格式、无法批量导出 | ❌ 否决（留 MCWS 适配位） |
| 全部委托 beets | 生态成熟 | 要建库、要联网、中文命名差、100 GB 首次导入很久 | ❌ 只作对照 |
| 自算 + 缓存 | 可并发、可缓存、数值精确、离线 | 口径需自己定义并文档化（已写在 README 的"已知边界"） | ✅ 采用 |
| 位对齐只调 CUETools GUI | 与官方读数一致 | 官方注明跑完不关闭、不能循环，做不了批处理；单曲无 TOC | ❌ 改为自己实现 AccurateRip（ADR-8） |
| 响度归一只报 LUFS 数字 | 零依赖 | 报数不能跨播放器生效；要写标准 ReplayGain 标签才有用 | ❌ 外挂随包 rsgain（ADR-9） |

对应架构决策：ADR-1（自算频谱）、ADR-2（本地 DR）、ADR-3（两遍解码）、ADR-4（先分组后深挖）、ADR-5（sqlite 缓存）、
ADR-8（自算 AccurateRip）、ADR-9（外挂 rsgain、默认只读）。
新增第 9/10 项（声道相关、DC）是**顺路算的**：本来就在同一遍解码里拿到了整轨样本，加 5 个点积即可，边际成本≈0。

---

## 7. 第 12 项"录音同一性"为什么没做，以及替代方案

Chromaprint/fpcalc + AcoustID 能回答"这两版是不是同一段录音"（比文件名可靠），但：
AcoustID 要联网、要申请 application key，且指纹库对中文曲目的元数据覆盖参差——
与本项目"纯本地、零注册、零 key"的约束冲突。ffmpeg essentials 版也不含 chromaprint 滤镜。

当前替代：`dsc/group.py` 用**歌名归一化 + 艺人相似 + 时长容差 + 文件头 1 MB 的 blake2b 快速哈希**
聚类，已能覆盖"同一首歌的多个版本"这一实际场景；快哈希相同的直接标记为 `duplicate_of`。
代价：改过标签或文件头不同的同一录音会被当成两版——但这种误判只在"同一录音不同封装"时出现，
不影响"该留哪版"的结论（反正都要比指标）。

如果以后需要专业级同一性判定：装 `beets` + `chromaprint` 做离线对照即可，本工具不内联它。

---

## 6. 引用

- Spek — https://spek.cc
- dr14_t.meter — https://github.com/simon-r/dr14_t.meter
- rsgain — https://github.com/complexlogic/rsgain
- loudgain — https://github.com/ghedo/loudgain
- beets — https://github.com/beetbox/beets
- pypinyin — https://github.com/mozillazg/python-pinyin
- mutagen — https://mutagen.readthedocs.io
- CUETools DB — https://github.com/gchudov/db.cue.tools
- Friture — https://github.com/tlecomte/friture
- ffmpeg filters（ebur128 / astats / showspectrumpic）— https://ffmpeg.org/ffmpeg-filters.html
