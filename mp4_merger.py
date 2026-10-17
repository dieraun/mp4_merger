#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mp4_merger.py — 视频批量合并工具 v4.5
更新要点:
  - 真正验证 GPU 编码器可用性（试编码 1 秒），避免"有编码器无驱动"全失败
  - 硬件编码运行时失败 → 自动回退 CPU 重试一次
  - --skip-existing 只跳过非空输出，避免残留 0 字节文件被误判
  - 修复 v4.4 中 args.video_copy 悬空引用
"""
import argparse
import ctypes
import json
import os
import re
import shutil
import stat as stat_mod
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

VERSION = "4.5"
DEFAULT_EXTS = {".webm", ".mp4", ".mkv", ".mov", ".flv", ".avi", ".ogg"}
DEFAULT_CRF = 16
DEFAULT_AUDIO_BITRATE = "320k"

_POPEN_CREATIONFLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
)
_print_lock = threading.Lock()


def safe_print(*args: Any, **kwargs: Any) -> None:
    kwargs.setdefault("flush", True)
    with _print_lock:
        print(*args, **kwargs)


def die(msg: str, code: int = 1) -> None:
    print(C.err(msg), file=sys.stderr, flush=True)
    sys.exit(code)


def _require_win10() -> None:
    if os.name != "nt":
        die("❌ 本工具仅支持 Windows 10 及以上。")
    try:
        v = sys.getwindowsversion()
    except AttributeError:
        die("❌ 无法识别 Windows 版本。")
    if v.major < 10:
        die("❌ 需要 Windows 10 及以上。")


# -------------------- 彩色输出 --------------------
class C:
    _on: bool = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    if _on:
        try:
            _k = ctypes.windll.kernel32
            _h = _k.GetStdHandle(-11)
            _mode = ctypes.c_uint32()
            if _k.GetConsoleMode(_h, ctypes.byref(_mode)):
                _k.SetConsoleMode(_h, _mode.value | 0x0004)
        except Exception:
            pass

    @classmethod
    def set_off(cls) -> None:
        cls._on = False

    @classmethod
    def _w(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls._on else s

    @classmethod
    def info(cls, s): return cls._w("36", s)
    @classmethod
    def ok(cls, s): return cls._w("32", s)
    @classmethod
    def warn(cls, s): return cls._w("33", s)
    @classmethod
    def err(cls, s): return cls._w("31", s)
    @classmethod
    def dim(cls, s): return cls._w("2", s)


# -------------------- 辅助工具 --------------------
def fs_str(p: Path) -> str:
    s = str(p.absolute())
    if len(s) < 248:
        return s
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def _decode_output(raw: Optional[bytes]) -> str:
    if not raw:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def find_tool(name: str, explicit: Optional[str] = None) -> str:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            die(f"❌ 指定的 {name} 路径无效: {p}")
        return fs_str(p)
    path = shutil.which(name)
    if not path:
        safe_print(C.warn(
            "\n📥 未找到 ffmpeg/ffprobe。Windows 安装建议：\n"
            "   1. winget install Gyan.FFmpeg\n"
            "   2. 或 https://www.gyan.dev/ffmpeg/builds/ 下载 full 版本\n"
            "   3. 或 --ffmpeg D:/path/ffmpeg.exe --ffprobe D:/path/ffprobe.exe\n"
        ))
        die(f"❌ 找不到 {name}。")
    return path


def verify_gpu_encoder(ffmpeg: str, encoder: str, prefix: str = "") -> bool:
    """实际编码 1 秒 320x240 测试源，能跑通才算硬件可用"""
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "lavfi",
        "-i", "testsrc=duration=1:size=320x240:rate=10",
        "-c:v", encoder, "-f", "null", "-",
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, timeout=30,
            creationflags=_POPEN_CREATIONFLAGS,
        )
        if res.returncode == 0:
            return True
        err = _decode_output(res.stderr).strip().splitlines()
        detail = err[-1] if err else f"exit={res.returncode}"
        safe_print(C.warn(f"{prefix}  ⚠ {encoder} 试编码失败: {detail}"))
        return False
    except subprocess.TimeoutExpired:
        safe_print(C.warn(f"{prefix}  ⚠ {encoder} 试编码超时(30s)"))
        return False
    except Exception as e:
        safe_print(C.warn(f"{prefix}  ⚠ {encoder} 试编码异常: {e}"))
        return False


def _fmt_eta(sec: float) -> str:
    sec = max(int(sec), 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# -------------------- 探测 --------------------
@dataclass
class VideoInfo:
    path: Path
    size: int = 0
    duration: float = 0.0
    v_codec: str = ""
    a_codec: str = ""
    has_audio: bool = False
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    bit_depth: int = 8
    error: str = ""


def _parse_bit_depth(pix_fmt: str) -> int:
    m = re.search(r"p(\d{1,2})(?:le|be)?$", pix_fmt or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return 8


def probe(ffprobe: str, path: Path) -> VideoInfo:
    info = VideoInfo(path=path)
    try:
        try:
            st = path.stat()
        except OSError:
            info.error = "文件不存在"
            return info
        if not stat_mod.S_ISREG(st.st_mode):
            info.error = "不是普通文件"
            return info
        info.size = st.st_size
        if info.size <= 0:
            info.error = "文件大小为0"
            return info

        out = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", fs_str(path)],
            capture_output=True, timeout=120,
            creationflags=_POPEN_CREATIONFLAGS,
        )
        if out.returncode != 0:
            err = _decode_output(out.stderr).strip().splitlines()
            info.error = err[-1] if err else f"ffprobe exit={out.returncode}"
            return info
        data = json.loads(_decode_output(out.stdout) or "{}")
        fmt = data.get("format", {})
        try:
            info.duration = float(fmt.get("duration", 0) or 0)
        except (TypeError, ValueError):
            info.duration = 0.0

        stream_durs: List[float] = []
        for st in data.get("streams", []):
            ctype = st.get("codec_type")
            if ctype == "video":
                disp = st.get("disposition") or {}
                if disp.get("attached_pic"):
                    continue
                if not info.v_codec:
                    info.v_codec = st.get("codec_name", "") or ""
                    info.width = int(st.get("width", 0) or 0)
                    info.height = int(st.get("height", 0) or 0)
                    info.pix_fmt = st.get("pix_fmt", "") or ""
                    info.bit_depth = _parse_bit_depth(info.pix_fmt)
                try:
                    d = float(st.get("duration", 0) or 0)
                    if d > 0:
                        stream_durs.append(d)
                except (TypeError, ValueError):
                    pass
            elif ctype == "audio":
                info.has_audio = True
                if not info.a_codec:
                    info.a_codec = st.get("codec_name", "") or ""
                try:
                    d = float(st.get("duration", 0) or 0)
                    if d > 0:
                        stream_durs.append(d)
                except (TypeError, ValueError):
                    pass
        if info.duration <= 0 and stream_durs:
            info.duration = max(stream_durs)
        if not info.v_codec:
            info.error = "无视频流"
    except subprocess.TimeoutExpired:
        info.error = "探测超时"
    except json.JSONDecodeError:
        info.error = "json解析失败"
    except Exception as e:
        info.error = f"{type(e).__name__}: {e}"
    return info


def probe_all(ffprobe: str, paths: List[Path], show_progress: bool) -> List[VideoInfo]:
    if not paths:
        return []

    def _size(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:
            return 0

    ordered = sorted(paths, key=_size, reverse=True)
    workers = min(8, (os.cpu_count() or 4) * 2, max(1, len(ordered)))
    results: List[VideoInfo] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(probe, ffprobe, p): p for p in ordered}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append(VideoInfo(path=futures[fut], error=f"线程异常: {exc}"))
            done += 1
            if show_progress and len(ordered) > 20 and done % 20 == 0:
                safe_print(C.dim(f"   探测进度 {done}/{len(ordered)}"))
    return results


# -------------------- 任务发现 --------------------
@dataclass
class MergeTask:
    folder: Path
    files: List[Path]
    out_path: Path
    infos: Optional[List[VideoInfo]] = None


def natural_key(s: str) -> List[Any]:
    parts = []
    for t in re.split(r"([0-9]+)", s):
        if t.isdigit():
            n = int(t)
            parts.append((0, n) if n < 10**18 else (1, t.lower()))
        else:
            parts.append((1, t.lower()))
    return parts


def _out_name_for(folder: Path, root: Path) -> str:
    if folder == root:
        return "merged"
    try:
        rel = folder.relative_to(root)
        return str(rel).replace("\\", "_").replace("/", "_")
    except ValueError:
        return folder.name


def discover_tasks(root: Path, recursive: bool, exts: Set[str],
                   out_dir: Optional[Path], flat_mode: bool,
                   out_ext: str = ".mp4") -> List[MergeTask]:
    tasks: List[MergeTask] = []
    root = root.resolve()
    out_resolved = out_dir.resolve() if out_dir else None
    prune_out = (
        out_resolved is not None
        and out_resolved != root
        and out_resolved.is_relative_to(root)
    )

    def _file_ok(f: Path) -> bool:
        if not f.is_file() or f.suffix.lower() not in exts:
            return False
        if prune_out and f.resolve().is_relative_to(out_resolved):
            return False
        return True

    if flat_mode:
        out_path = (out_dir or root) / f"merged{out_ext}"
        files = [
            f for f in (root.rglob("*") if recursive else root.iterdir())
            if _file_ok(f) and f.resolve() != out_path.resolve()
        ]
        files.sort(key=lambda p: natural_key(p.name))
        if files:
            tasks.append(MergeTask(folder=root, files=files, out_path=out_path))
        return tasks

    if recursive:
        dirs: List[Path] = []
        for dp, dns, _ in os.walk(root):
            dp_path = Path(dp)
            if prune_out and dp_path.resolve() == out_resolved:
                dns[:] = []
                continue
            dirs.append(dp_path)
    else:
        dirs = [root] + [
            d for d in root.iterdir()
            if d.is_dir() and not (prune_out and d.resolve() == out_resolved)
        ]

    for folder in dirs:
        try:
            files = [f for f in folder.iterdir() if _file_ok(f)]
        except OSError:
            continue
        if not files:
            continue
        files.sort(key=lambda p: natural_key(p.name))

        if out_dir:
            out_path = out_dir / f"{_out_name_for(folder, root)}{out_ext}"
        else:
            out_path = folder / f"merged{out_ext}"
        files = [f for f in files if f.resolve() != out_path.resolve()]
        if files:
            tasks.append(MergeTask(folder=folder, files=files, out_path=out_path))
    return tasks


# -------------------- ffmpeg 执行 --------------------
def _concat_escape(s: str) -> str:
    return s.replace("'", "'\\''")


def run_ffmpeg(cmd: List[str], total_duration: float, label: str,
               show_progress: bool, prefix: str) -> int:
    """执行 ffmpeg，返回退出码 (0=成功)"""
    err_path: Optional[Path] = None
    proc: Optional[subprocess.Popen] = None
    rc = -1
    t0 = time.time()
    last_pct = -1.0
    show_progress = show_progress and sys.stdout.isatty()

    def update_progress(us: int):
        nonlocal last_pct, total_duration
        if not show_progress:
            return
        cur_sec = us / 1_000_000
        if cur_sec > total_duration:
            total_duration = cur_sec * 1.05
        if total_duration > 1e-6:
            pct = min(cur_sec / total_duration * 100.0, 100.0)
            if pct >= last_pct + 0.5 or pct >= 100.0:
                last_pct = pct
                elapsed = max(time.time() - t0, 1e-3)
                speed = cur_sec / elapsed
                eta = max(total_duration - cur_sec, 0) / speed if speed > 0 else 0
                filled = int(25 * pct / 100.0)
                bar = "█" * filled + "░" * (25 - filled)
                print(f"\r{prefix}    └─ {label} [{bar}] {pct:5.1f}%  "
                      f"{speed:4.1f}x  ETA {_fmt_eta(eta)}  ",
                      end="", flush=True)
        else:
            print(f"\r{prefix}    └─ {label} 处理中... {time.time()-t0:4.1f}s",
                  end="", flush=True)

    try:
        err_fd = tempfile.NamedTemporaryFile(
            mode="w+b", suffix=".log", delete=False)
        err_path = Path(err_fd.name)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=err_fd,
                stdin=subprocess.DEVNULL, bufsize=0,
                creationflags=_POPEN_CREATIONFLAGS,
            )
            if proc.stdout:
                for raw in iter(proc.stdout.readline, b""):
                    line = _decode_output(raw).strip()
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k in ("out_time_us", "out_time_ms"):
                            try:
                                update_progress(int(v))
                            except ValueError:
                                pass
            proc.wait()
            rc = proc.returncode
        finally:
            try:
                err_fd.close()
            except Exception:
                pass
    except KeyboardInterrupt:
        if proc:
            _kill_proc_tree(proc)
        raise
    finally:
        if show_progress:
            print()

    if rc != 0 and err_path:
        try:
            with open(err_path, "rb") as f:
                tail = "\n".join(
                    _decode_output(b) for b in f.read().splitlines()[-8:]
                )
        except Exception:
            tail = ""
        safe_print(C.err(f"{prefix}    └─ ffmpeg 退出码 {rc}"))
        if tail:
            safe_print(C.dim(tail))
    if err_path:
        try:
            err_path.unlink(missing_ok=True)
        except Exception:
            pass
    return rc


# -------------------- 单任务执行 --------------------
def run_one_task(ffmpeg: str, task: MergeTask, root: Path, args,
                 show_progress: bool, prefix: str,
                 use_gpu: Optional[str], concurrent: bool) -> Tuple[str, str]:
    try:
        rel = task.folder.relative_to(root)
    except ValueError:
        rel = task.folder
    safe_print(C.info(f"\n--- {prefix}[任务] {rel} ---"))

    # v4.5: 跳过已存在且非空输出，避免上次失败残留的 0 字节文件被误判
    if args.skip_existing and task.out_path.exists() \
            and task.out_path.stat().st_size > 0:
        safe_print(C.dim(f"{prefix}  ⏭ 输出已存在，跳过: {task.out_path.name}"))
        return ("skip", "skip-existing")

    all_infos = task.infos or []
    for bad in (i for i in all_infos if i.error):
        safe_print(C.warn(f"{prefix}  ⚠ 跳过异常文件: {bad.path.name} ({bad.error})"))
    valid_infos = [i for i in all_infos if not i.error]
    if not valid_infos:
        return ("fail", "no-valid-input")

    total_duration = sum(i.duration for i in valid_infos)
    safe_print(f"{prefix}  📄 文件数: {len(valid_infos)} | "
               f"总时长: {_fmt_eta(total_duration)}")

    if task.out_path.exists() and not args.yes and not concurrent:
        resp = input(C.warn(
            f"⚠ 文件已存在: {task.out_path}\n是否覆盖? [y/n] ")).strip().lower()
        if resp not in ("y", "yes"):
            return ("skip", "user-skip")

    tmpdir = Path(tempfile.mkdtemp(prefix="mp4merge_"))
    list_file = tmpdir / "concat.txt"

    has_audios = [i.has_audio for i in valid_infos]
    has_any_audio = any(has_audios)
    has_all_audio = all(has_audios)
    need_silence_patch = has_any_audio and not has_all_audio

    if need_silence_patch:
        safe_print(f"{prefix}  🛠 检测到音轨混搭(有/无)，正在预生成静音音轨防错位...")

    try:
        # ---------- 写 concat 列表 ----------
        with open(list_file, "w", encoding="utf-8") as f:
            for idx, info in enumerate(valid_infos):
                use_path = info.path
                if need_silence_patch and not info.has_audio:
                    temp_silent = tmpdir / f"silent_{idx}.mkv"
                    dur = max(info.duration, 0.0)
                    cmd_pad = [
                        ffmpeg, "-hide_banner", "-nostdin", "-y",
                        "-i", fs_str(info.path),
                        "-f", "lavfi", "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=48000",
                    ]
                    if dur > 1e-6:
                        cmd_pad += ["-t", f"{dur:.6f}"]
                    cmd_pad += [
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-map", "0:v:0", "-map", "1:a:0",
                        fs_str(temp_silent),
                    ]
                    pad = subprocess.run(
                        cmd_pad, capture_output=True,
                        creationflags=_POPEN_CREATIONFLAGS,
                    )
                    if pad.returncode != 0 or not temp_silent.exists() \
                            or temp_silent.stat().st_size == 0:
                        err = _decode_output(pad.stderr)[-300:]
                        safe_print(C.err(
                            f"{prefix}  ❌ 静音补全失败: {info.path.name}\n{err}"))
                        return ("fail", "silent-pad-failed")
                    use_path = temp_silent
                f.write(
                    f"file '{_concat_escape(Path(os.path.abspath(use_path)).as_posix())}'\n"
                )
                if info.duration > 1e-6:
                    f.write(f"duration {info.duration:.6f}\n")

        # ---------- 视频参数 ----------
        common = [ffmpeg, "-hide_banner", "-nostdin",
                  "-progress", "pipe:1", "-nostats", "-y"]

        if args.true_lossless:
            v_args = ["-c:v", "libx264", "-qp", "0"]
            pix_args: List[str] = []
            enc_name = "CPU (绝对数学无损)"
            safe_print(f"{prefix}  ⚠️ 无损模式，输出写入 .mov")
        elif use_gpu == "nvidia":
            preset = "p1" if args.fast else "p4"
            v_args = ["-c:v", "h264_nvenc", "-preset", preset,
                      "-rc", "vbr", "-cq", str(args.crf), "-b:v", "0"]
            pix_args = _decide_pix_args(valid_infos, args)
            enc_name = "NVENC (N卡 GPU)"
        elif use_gpu == "intel":
            preset = "veryfast" if args.fast else "medium"
            v_args = ["-c:v", "h264_qsv", "-preset", preset,
                      "-global_quality", str(args.crf)]
            pix_args = _decide_pix_args(valid_infos, args)
            enc_name = "QSV (Intel 核显)"
        elif use_gpu == "amd":
            quality = "speed" if args.fast else "balanced"
            v_args = ["-c:v", "h264_amf", "-quality", quality,
                      "-qp_i", str(args.crf), "-qp_p", str(args.crf)]
            pix_args = _decide_pix_args(valid_infos, args)
            enc_name = "AMF (A卡 GPU)"
        else:
            cores = os.cpu_count() or 4
            cpu_preset = "veryfast" if (args.fast or cores <= 4) else "slow"
            v_args = ["-c:v", "libx264", "-preset", cpu_preset,
                      "-crf", str(args.crf)]
            pix_args = _decide_pix_args(valid_infos, args)
            enc_name = f"CPU (x264 {cpu_preset})"

        # ---------- 音频参数 ----------
        if args.true_lossless:
            a_args = ["-c:a", "pcm_s16le"]
        else:
            a_args = ["-c:a", "aac", "-b:a", args.audio_bitrate,
                      "-ar", "48000", "-ac", "2"]

        safe_print(f"{prefix}  🚀 合并 | 视频: {enc_name} | 音频: "
                   f"{'PCM' if args.true_lossless else 'AAC ' + args.audio_bitrate}"
                   f" | 色深: {'保留源' if not pix_args else '8bit 420'}"
                   f" | 容器: {task.out_path.suffix.lstrip('.').upper()}")

        cmd = common + [
            "-f", "concat", "-safe", "0", "-i", fs_str(list_file),
            "-map", "0:v:0", "-map", "0:a:0?",
            *pix_args,
            *v_args,
            *a_args,
            "-movflags", "+faststart",
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "9999",
            fs_str(task.out_path),
        ]

        rc = run_ffmpeg(cmd, total_duration, "合并", show_progress, prefix)

        # ---------- v4.5: 硬件编码运行时失败 → 自动 CPU 回退 ----------
        if rc != 0 and use_gpu is not None:
            # 用"输出文件 0 字节或不存在"作为硬件初始化失败的特征
            cur_size = task.out_path.stat().st_size if task.out_path.exists() else 0
            if cur_size == 0:
                safe_print(C.warn(
                    f"{prefix}  ⚠ 硬件编码失败({use_gpu})，自动回退 CPU 重试..."))
                cores = os.cpu_count() or 4
                cpu_preset = "veryfast" if (args.fast or cores <= 4) else "slow"
                cpu_cmd = common + [
                    "-f", "concat", "-safe", "0", "-i", fs_str(list_file),
                    "-map", "0:v:0", "-map", "0:a:0?",
                    *pix_args,
                    "-c:v", "libx264", "-preset", cpu_preset,
                    "-crf", str(args.crf),
                    *a_args,
                    "-movflags", "+faststart",
                    "-avoid_negative_ts", "make_zero",
                    "-max_muxing_queue_size", "9999",
                    fs_str(task.out_path),
                ]
                rc = run_ffmpeg(cpu_cmd, total_duration, "合并(CPU回退)",
                                show_progress, prefix)
                if rc == 0 and task.out_path.exists() \
                        and task.out_path.stat().st_size > 0:
                    safe_print(C.ok(
                        f"{prefix}  ✅ CPU 回退成功 -> {task.out_path.name}"))
                    return ("ok", "cpu-fallback")

        if rc == 0 and task.out_path.exists() and task.out_path.stat().st_size > 0:
            safe_print(C.ok(f"{prefix}  ✅ 完成 -> {task.out_path.name}"))
            return ("ok", "transcode")

        safe_print(C.err(f"{prefix}  ❌ 合并或转码失败"))
        return ("fail", "merge-failed")
    finally:
        shutil.rmtree(fs_str(tmpdir), ignore_errors=True)


def _decide_pix_args(infos: List[VideoInfo], args) -> List[str]:
    """全 10bit 源 或 无损模式 → 保留源色深；否则强制 8bit 420 兼容"""
    if args.true_lossless:
        return []
    all_10bit = all(i.bit_depth >= 10 for i in infos if i.pix_fmt)
    if all_10bit and not args.force_8bit:
        return []
    return ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"]


# -------------------- 主入口 --------------------
def build_parser():
    p = argparse.ArgumentParser(
        description=f"MP4 高画质合并工具 v{VERSION}")
    p.add_argument("folder", help="主目录")
    p.add_argument("-o", "--output-dir", help="输出目录")
    p.add_argument("--flat", action="store_true", help="平铺模式")
    p.add_argument("-r", "--recursive", action="store_true", help="递归子目录")
    p.add_argument("--cpu", action="store_true", help="强制 CPU")
    p.add_argument("--fast", action="store_true", help="快速模式 (CRF 23)")
    p.add_argument("--true-lossless", action="store_true",
                   help="绝对数学无损（自动输出 .mov）")
    p.add_argument("--force-8bit", action="store_true",
                   help="强制 8bit yuv420p")
    p.add_argument("--crf", type=int, default=DEFAULT_CRF)
    p.add_argument("--audio-bitrate", default=DEFAULT_AUDIO_BITRATE)
    p.add_argument("--ext", default=",".join(sorted(DEFAULT_EXTS)))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--skip-existing", action="store_true")
    p.add_argument("-j", "--jobs", type=int, default=1)
    p.add_argument("--ffmpeg")
    p.add_argument("--ffprobe")
    p.add_argument("--version", action="version", version=VERSION)
    return p


def main() -> int:
    args = build_parser().parse_args()
    _require_win10()
    if args.no_color:
        C.set_off()
    if args.jobs == 0:
        args.jobs = max(1, (os.cpu_count() or 4) // 2)

    if args.fast:
        if args.crf == DEFAULT_CRF:
            args.crf = 23
        if args.audio_bitrate == DEFAULT_AUDIO_BITRATE:
            args.audio_bitrate = "192k"

    if args.true_lossless and args.audio_bitrate != DEFAULT_AUDIO_BITRATE:
        safe_print(C.warn("⚠ --true-lossless 下 --audio-bitrate 被忽略。"))

    ffmpeg = find_tool("ffmpeg", args.ffmpeg)
    ffprobe = find_tool("ffprobe", args.ffprobe)

    # ---------- v4.5: GPU 探测 + 试编码验证 ----------
    use_gpu: Optional[str] = None
    if not args.cpu and not args.true_lossless:
        try:
            enc_out = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True, encoding="utf-8",
                errors="ignore", creationflags=_POPEN_CREATIONFLAGS).stdout
        except Exception:
            enc_out = ""

        candidates: List[Tuple[str, str]] = []
        if "h264_nvenc" in enc_out:
            candidates.append(("nvidia", "h264_nvenc"))
        if "h264_qsv" in enc_out:
            candidates.append(("intel", "h264_qsv"))
        if "h264_amf" in enc_out:
            candidates.append(("amd", "h264_amf"))

        if candidates:
            safe_print(C.info("🔬 正在验证硬件编码器（各试编码 1 秒 320x240）..."))
            for gpu_key, encoder in candidates:
                if verify_gpu_encoder(ffmpeg, encoder):
                    use_gpu = gpu_key
                    safe_print(C.ok(f"   ✅ {encoder} 可用，已启用"))
                    break
                else:
                    safe_print(C.dim(f"   ✗ {encoder} 不可用，跳过"))
            if use_gpu is None:
                safe_print(C.warn(
                    "⚠ 所有硬件编码器均不可用，已自动回退 CPU。\n"
                    "   常见原因：无对应显卡 / 驱动过旧 / nvcuda.dll 缺失"))

    # NVENC 消费卡 session 上限：自动 cap
    if use_gpu == "nvidia" and args.jobs > 2:
        safe_print(C.warn(
            f"⚠ 检测到 NVENC + -j {args.jobs}：消费卡通常只能 2 路编码，"
            f"已自动降为 -j 2（如需更多请 --cpu 或手动改代码）。"))
        args.jobs = 2

    gpu_name = {"nvidia": "NVIDIA (NVENC)", "intel": "Intel (QSV)",
                "amd": "AMD (AMF)"}.get(use_gpu, "未检测到 (CPU)")
    if args.true_lossless:
        gpu_name = "CPU (无损模式)"
    safe_print(C.info(
        f"🚀 加速: {gpu_name} | 画质: "
        f"{'QP=0' if args.true_lossless else 'CRF=' + str(args.crf)}"))

    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        die(f"❌ 目录不存在: {root}")
    out_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    exts = {e.strip().lower() if e.strip().startswith(".")
            else "." + e.strip().lower()
            for e in args.ext.split(",") if e.strip()}
    out_ext = ".mov" if args.true_lossless else ".mp4"
    tasks = discover_tasks(root, args.recursive, exts, out_dir, args.flat, out_ext)
    if not tasks:
        die("❌ 未找到任何符合要求的视频文件。")
    safe_print(f"🔍 找到 {len(tasks)} 个合并任务。\n")

    if args.dry_run:
        for i, t in enumerate(tasks, 1):
            safe_print(f"  [{i}/{len(tasks)}] {t.folder} -> {t.out_path}")
        return 0

    concurrent = args.jobs > 1
    if concurrent and not args.yes:
        safe_print(C.warn("⚠ 并发模式自动 --yes。"))
        args.yes = True

    safe_print(C.info("🔎 正在探测所有文件信息..."))
    infos = probe_all(ffprobe, [f for t in tasks for f in t.files], not concurrent)
    info_map = {i.path: i for i in infos}
    for t in tasks:
        t.infos = [info_map[f] for f in t.files if f in info_map]

    success_count = fail_count = skip_count = 0
    failed_paths: List[Tuple[Path, str]] = []
    show_progress = not concurrent

    def _agg(res, task: MergeTask):
        nonlocal success_count, fail_count, skip_count
        if res[0] == "ok":
            success_count += 1
        elif res[0] == "skip":
            skip_count += 1
        else:
            fail_count += 1
            failed_paths.append((task.folder, res[1]))

    try:
        if concurrent:
            with ThreadPoolExecutor(max_workers=args.jobs) as ex:
                futures = {
                    ex.submit(run_one_task, ffmpeg, t, root, args, False,
                              f"[{i}/{len(tasks)}] ", use_gpu, True): t
                    for i, t in enumerate(tasks, 1)
                }
                for fut in as_completed(futures):
                    try:
                        _agg(fut.result(), futures[fut])
                    except Exception as e:
                        fail_count += 1
                        failed_paths.append((futures[fut].folder, f"异常: {e}"))
                        safe_print(C.err(f"任务异常: {e}"))
        else:
            for i, t in enumerate(tasks, 1):
                try:
                    _agg(run_one_task(ffmpeg, t, root, args, show_progress,
                                      f"[{i}/{len(tasks)}] ", use_gpu, False), t)
                except KeyboardInterrupt:
                    safe_print(C.warn("\n⚠ 已中断"))
                    return 130
    except KeyboardInterrupt:
        safe_print(C.warn("\n⚠ 已中断"))
        return 130

    total = success_count + fail_count + skip_count
    safe_print("\n" + "=" * 50)
    safe_print(C.ok("✅ 批量处理完成！"))
    safe_print(f"   总计: {total} | 成功: {success_count} | "
               f"失败: {fail_count} | 跳过: {skip_count}")
    if failed_paths:
        safe_print(C.warn(f"\n⚠ 失败清单 ({len(failed_paths)}):"))
        for p, reason in failed_paths[:20]:
            safe_print(C.dim(f"   - {p}  [{reason}]"))
        if len(failed_paths) > 20:
            safe_print(C.dim(f"   ... 及另外 {len(failed_paths)-20} 项"))
    safe_print("=" * 50)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())