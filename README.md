# rpf-audio-dump

把 GTA VI（Xbox 版）`audio.rpf` 里的**全部**音频一次性导出成可直接播放的文件：一个 bank 一个文件夹，文件名就是 stream id。

纯 Python 标准库实现，**不需要 ffmpeg、不需要 SixNG、不需要任何第三方库**。能不解码就不解码：Opus 包原样封进 Ogg 容器，XMA2 数据原样封进 RIFF/WAVE，转码只在你自己加 `--ffmpeg` 时才发生。

---

## 它导出什么

`audio.rpf` 里共有 7,160 个 ADAT bank、1,606,241 条流，分四种布局：

| 布局 | 数量 | 真实编码 | 本工具输出 | 是否转码 |
|---|---|---|---|---|
| `blocks` | 1,598,565 | Opus（2 KiB 块内 `[u16 长度][包]`） | `<stream id>.opus` | 否，原包封装 |
| `music` | 3,008 | Opus（512 KiB 页 / 2 KiB 块，按块交错） | `<stream id>.opus` 或 `<stream id>_ch1..N.opus` | 否，原包封装 |
| `other` | 1,550 | XMA2（32 kHz，2048 字节一块） | `<stream id>.xma.wav` | 否，只加 RIFF 头 |
| `none` | 3,118 | 无数据（占位） | 不写 | — |

说明：

- **music 多声道**：8 声道 bank 其实是 4 条立体声子流按块交错，4 声道 bank 是 4 条单声道子流。所以多声道 bank 会拆成 `_ch1.opus`、`_ch2.opus`……每条都是原始 Opus 包，零重编码（6 声道以下请用播放器合并，或自己加 `--ffmpeg` 转）。
- **other / XMA2**：包里就是 XMA2 数据，加 34 字节 `XMA2WAVEFORMATEX` 头后可直接播放，但需要支持 XMA2 的解码器（PotPlayer / mpv / MPC-HC / foobar2000 等 ffmpeg 系，VLC 与 Windows 自带播放器不支持）。加 `--ffmpeg` 可自动转成 mp3。
- 约 91 条 blocks 流在归档里本身就是空的（`no Opus packets`），任何工具都导不出声音。

## 输出结构

```
D:\out\
├─ 129b000\
│  ├─ 0211d462.opus
│  └─ ...
├─ 6927a8000\
│  ├─ 00000000_ch1.opus
│  ├─ 00000000_ch2.opus
│  ├─ 00000000_ch3.opus
│  └─ 00000000_ch4.opus
├─ 69dbc6000\
│  ├─ 026f4e1c.xma.wav
│  └─ ...
├─ _index.json.gz      ← 扫描结果缓存（下次直接复用）
├─ _summary.json
└─ _failed.txt         ← 只有出错时才生成
```

每个 bank 文件夹里还有一个 `.done` 标记文件，记录该 bank 导出的文件数与字节数；重跑时自动跳过已完成的 bank（`--force` 可强制重写）。

## 快速开始

```bat
:: 1) 先看看会导出多少东西（不写文件）
python rpf_audio_dump.py --rpf "C:\path\to\audio.rpf" --out D:\out --dry-run

:: 2) 正式全量导出（索引只扫一次，约 3.5 分钟；之后并行导出）
python rpf_audio_dump.py --rpf "C:\path\to\audio.rpf" --out D:\out

:: 3) 只要 60 秒以上的、并且顺手把 XMA2 转成 mp3
python rpf_audio_dump.py --rpf "C:\path\to\audio.rpf" --out D:\out --min-seconds 60 --ffmpeg ffmpeg
```

Windows 下也可以直接双击 `run.cmd`（需要把 `AUDIO_RPF` 与 `OUT_DIR` 两个变量改成你自己的路径）。

## 参数

| 参数 | 说明 |
|---|---|
| `--rpf PATH` | `audio.rpf` 路径，默认自动在脚本同级目录找 |
| `--out, -o DIR` | 输出根目录（必填，一个 bank 一个子文件夹） |
| `--jobs, -j N` | 并行 bank 数，默认 `min(16, CPU×2)` |
| `--min-seconds S` | 跳过短于 S 秒的流（同 SixNG 的 Min length） |
| `--banks A,B` | 只导指定 bank（十六进制偏移，逗号分隔） |
| `--limit N` | 只导前 N 个 bank |
| `--index FILE` | 指定索引缓存路径（默认 `<out>\_index.json.gz`） |
| `--rescan` | 忽略缓存，重新扫描归档 |
| `--index-only` | 只扫描并报告数量，不导出 |
| `--no-blocks` / `--no-music` / `--no-xma` | 跳过某一类流 |
| `--ffmpeg PATH` | 可选：把 XMA2 转成 mp3/flac/wav（默认不转） |
| `--xma-format` | `mp3`（默认）/ `flac` / `wav` |
| `--bitrate` | mp3 码率，默认 `192k` |
| `--no-keep-xma` | 转码后删除 `.xma.wav` |
| `--force` | 忽略 `.done`，强制重写 |
| `--dry-run` | 只打印计划，不写文件 |
| `--verify N` | 导出后随机抽 N 个文件做结构校验 |

## 性能

在 NVMe + 16 线程机器上实测（26.6 GiB 的 `audio.rpf`）：

- 扫描归档、建立索引：**204 秒**（顺序 mmap 扫 26.6 GiB，命中 7,174 个 `ADAT` 魔数），结果缓存后下次 0 秒。
- 导出：**约 1,000 个文件/秒**（300 个 bank / 5,946 个文件 / 5.6 秒）。
- 全量 1,603,180 个文件约 **25–35 分钟**，总体积约 **24 GB**（Opus 18.4 GB + music 5.5 GB + XMA2 0.1 GB）。
- 瓶颈在文件系统的小文件写入，不在解析；`--jobs` 开到 8–16 收益明显，再往上收益递减。

## 与 SixNG 的一致性

本工具的 ADAT 解析逻辑移植自 [SixNG](https://github.com/.../SixNG)（Rust，`crates/sixng-core/src/audio.rs`），并做了逐项比对：

- 7,160 个 bank、1,606,241 条流的 **layout 分类完全一致**（blocks 1,598,565 / music 3,008 / other 1,550 / none 3,118）。
- 前 60 个 bank 的 stream id、layout、data 偏移 **0 处不一致**。
- 同一条流（`129b000/0211d462`）解出的 PCM 与 SixNG 导出的文件**逐样本相同**（最大差值 0），时长同为 1.271938 秒。
- 8 声道 bank `6927a8000`：SixNG 显示 `15:41.8 / 8 ch`，本工具拆成 4 条 941.88 秒的立体声，时长与声道数吻合。

## 已知限制

- music 多声道是「拆子流」而不是单个多声道文件（多流 Ogg 在多数播放器上不可移植，合并需要解码重编码）。
- XMA2 需要 ffmpeg 系解码器；没有 ffmpeg 时只保证**数据完整**，不保证所有播放器能播。
- music 中 `2fd0ca000` 这一条流的 data 里混有索引表字节，会产生 1–2 个假包；工具用 RFC 6716 帧框架校验把它们丢掉，其余 3,007 条 music 流完全干净。
- 只支持这一版 GTA VI（Xbox 1.0.2.1）的 `audio.rpf`；其它版本的 ADAT 布局若不同，需要调整 `parse_header` / `fill_formats`。

## 许可

MIT
