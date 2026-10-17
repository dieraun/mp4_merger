```
# mp4_merger.py 🎬
> Windows 平台批量视频合并转码工具，基于 ffmpeg，版本 v4.5

[![GitHub stars](https://img.shields.io/github/stars/你的用户名/mp4_merger.svg?style=social)](https://github.com/你的用户名/mp4_merger)

一个面向本地大量视频文件的批量合并脚本，适合本地素材归档、录播片段拼接、分集视频批量合成。自动扫描文件夹，支持目录批量任务，硬件加速编码，自动容错降级。

## ✨ 主要亮点
1. **硬件编码自动检测与校验**
会真实做短片段测试编码，不是单纯看ffmpeg编码器列表；硬件编码运行出错会自动回退CPU重新编码，避免整个任务直接报废。支持 NVIDIA NVENC / Intel QSV / AMD AMF。消费级N卡自动限制并发编码路数，规避NVENC会话上限报错。

2. **智能处理音轨不一致问题**
部分源文件无音频、部分有音频时，自动生成静音填充音轨，防止合并后音画错位、时间轴错乱问题。

3. **丰富的扫描模式**
- 普通模式：每个子文件夹输出一个合并视频
- `--flat` 平铺模式：把整个目录所有视频全部合并成一个文件
- 支持递归扫描子目录，自定义视频后缀，输出文件可以统一输出到外部文件夹，不污染源目录。

4. **完善的跳过与覆盖逻辑**
`--skip-existing` 会判断输出文件是否非空，不会把上次失败产生的0字节空文件直接跳过；支持dry-run预演，只打印任务列表不实际渲染，方便先确认任务。支持-y自动覆盖交互。

5. **进度显示与错误收集**
带进度条、编码速度、剩余ETA预估；ffmpeg错误日志自动捕获，只打印关键错误片段，不会刷屏；全部任务结束后汇总成功/失败/跳过统计，输出失败任务清单，方便排查问题。

6. **多种画质模式可选**
CRF可调，fast快速模式；支持强制8bit兼容输出；支持数学无损模式输出mov；音频参数可自定义码率。

7. **并发批量处理**
支持-j多任务并发，多文件夹同时编码，充分利用硬件性能。

## ⚠️ 注意事项 & 局限
> 本工具主要面向 Windows10 及以上系统。
> 其他操作系统未做适配，部分系统API、进程处理逻辑会失效，不建议直接在其它平台运行，运行可能会出现各种不可预期异常。

- 仅支持H.264输出；无损模式输出mov容器
- 硬件编码效果高度依赖显卡驱动版本，驱动异常会自动降级CPU
- 并发硬件编码受显卡本身编码器会话限制，消费显卡同时编码路数有限
- 只做视频合并转码，不做剪辑、分割功能

## 📦 依赖
1. Python 3.10 及以上
2. ffmpeg + ffprobe
    - winget 安装：`winget install Gyan.FFmpeg`
    - 或者下载完整ffmpeg构建包，手动指定路径参数使用
3. Windows 10 / Windows11

## 🚀 使用示例
```bash
# 当前目录，每个子文件夹分别合并，递归扫描
python mp4_merger.py ./videos -r

# 平铺模式：全部文件合并成一个视频，输出到out文件夹
python mp4_merger.py ./videos --flat -o ./out

# 快速模式，启用硬件加速，跳过已完成文件
python mp4_merger.py ./videos -r --fast --skip-existing

# 预演，只看会生成哪些任务，不执行渲染
python mp4_merger.py ./videos -r --dry-run

# 强制使用CPU编码，4并发任务
python mp4_merger.py ./videos -r --cpu -j4
```

### 参数简要说明

```
positional arguments:
  folder                需要扫描处理的根目录

options:
  -o, --output-dir      指定输出文件夹
  --flat                平铺模式，全部视频合并为单个文件
  -r, --recursive       递归扫描子文件夹
  --cpu                 强制关闭硬件加速，只用CPU
  --fast                快速编码预设
  --true-lossless       无损输出，容器为mov
  --force-8bit          强制输出8bit yuv420p
  --crf                 设置crf画质参数，默认16
  --audio-bitrate       音频码率设置
  --ext                 自定义识别的视频后缀
  --dry-run             仅预览任务，不实际执行
  -y, --yes             自动覆盖输出文件
  --skip-existing       跳过已经存在且非空的输出文件
  -j, --jobs            并发任务数量
  --ffmpeg              指定ffmpeg.exe完整路径
  --ffprobe             指定ffprobe.exe完整路径
```