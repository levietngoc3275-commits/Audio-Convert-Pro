import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError as exc:
    raise SystemExit(
        "Thiếu thư viện customtkinter.\n\n"
        "Cài bằng lệnh:\n"
        "pip install customtkinter"
    ) from exc

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False


# ==============================================================================
# CONFIGURATION
# ==============================================================================

APP_TITLE = "Audio Converter Pro"
APP_VERSION = "2.3.1"

SETTINGS_FILE = Path.home() / ".audio_converter_pro.json"

SUPPORTED_FORMATS = ["MP3", "FLAC", "AAC", "WAV", "OGG", "M4A", "ALAC"]

INPUT_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".wave", ".m4a", ".m4b", ".aac", ".ogg",
    ".oga", ".opus", ".wma", ".alac", ".wv", ".aiff", ".aif", ".ape",
}

SAMPLE_RATES_ALL = [
    "Auto", "44100 Hz", "48000 Hz", "88200 Hz",
    "96000 Hz", "176400 Hz", "192000 Hz",
]
SAMPLE_RATES_MP3 = ["Auto", "44100 Hz", "48000 Hz"]
BITRATES = ["Auto", "128 kbps", "192 kbps", "256 kbps", "320 kbps"]
CONFLICT_POLICIES = ["Overwrite", "Skip", "Rename"]

DEFAULT_SETTINGS = {
    "format": "MP3",
    "bitrate": "Auto",
    "sample_rate": "Auto",
    "output_dir": str(Path.home() / "Music"),
    "conflict_policy": "Rename",
    "geometry": "1240x860",
}

_BITRATE_RE = re.compile(r"^(\d{1,4})\s*(?:k|kbps|kbit/s)?$", re.IGNORECASE)
WORKER_CLOSE_TIMEOUT_SECONDS = 5.0
_DND_HIGHLIGHT_COLOR = "#4f8cff"
_FULL_LOG_MAX_LINES = 50_000
_FULL_LOG_TRIM_CHUNK = 10_000
_LOG_VISIBLE_MAX_LINES = 400
_LOG_VISIBLE_TRIM_CHUNK = 200


def parse_bitrate_kbps(value: str) -> Optional[int]:
    if not value:
        raise ValueError("Bitrate rỗng.")
    v = value.strip()
    if v in {"Auto", "N/A (Lossless)"}:
        return None
    match = _BITRATE_RE.match(v)
    if not match:
        raise ValueError(
            f"Bitrate không hợp lệ: {value!r}. "
            "Chỉ chấp nhận 'Auto', 'N/A (Lossless)' hoặc dạng '128 kbps'."
        )
    kbps = int(match.group(1))
    if not (8 <= kbps <= 512):
        raise ValueError(f"Bitrate ngoài phạm vi hợp lệ (8-512 kbps): {kbps} kbps.")
    return kbps


def format_duration(seconds: float) -> str:
    try:
        s = int(max(0.0, float(seconds)))
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


# ==============================================================================
# DATA MODELS
# ==============================================================================

@dataclass
class AudioProbe:
    stream: Dict = field(default_factory=dict)
    format_info: Dict = field(default_factory=dict)
    cover_stream_index: Optional[int] = None

    @property
    def codec(self) -> str:
        return str(self.stream.get("codec_name") or "").lower()

    @property
    def sample_rate(self) -> int:
        try:
            return int(self.stream.get("sample_rate") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def channels(self) -> int:
        try:
            return int(self.stream.get("channels") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def sample_format(self) -> str:
        return str(self.stream.get("sample_fmt") or "").lower()

    @property
    def bits_per_raw_sample(self) -> int:
        try:
            return int(self.stream.get("bits_per_raw_sample") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def bits_per_sample(self) -> int:
        try:
            return int(self.stream.get("bits_per_sample") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def duration(self) -> float:
        value = self.format_info.get("duration")
        if value is None:
            value = self.stream.get("duration")
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


@dataclass
class ConversionProfile:
    target_format: str
    bitrate: str
    sample_rate: str
    conflict_policy: str

    @property
    def format_lower(self) -> str:
        return self.target_format.lower()

    @property
    def extension(self) -> str:
        if self.format_lower == "alac":
            return ".m4a"
        return f".{self.format_lower}"


@dataclass
class ConversionTask:
    source_path: Path
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "Pending"
    progress: float = 0.0
    error_message: str = ""
    enabled: bool = True
    _source_duration: float = field(init=False, default=0.0)

    def __post_init__(self):
        self.source_path = self.source_path.resolve()


# ==============================================================================
# AUDIO ENGINE
# ==============================================================================

class AudioEngine:

    @staticmethod
    def _find_binary(name: str) -> Optional[str]:
        path = shutil.which(name)
        if path:
            return path
        is_win = os.name == "nt"
        binary_names = [f"{name}.exe", name] if is_win else [name]
        base_dir = Path(__file__).resolve().parent
        candidates = []
        for b_name in binary_names:
            candidates.append(base_dir / b_name)
            candidates.append(base_dir / "bin" / b_name)
        for candidate in candidates:
            if candidate.exists():
                return str(candidate.resolve())
        return None

    @classmethod
    def get_ffmpeg_path(cls) -> Optional[str]:
        return cls._find_binary("ffmpeg")

    @classmethod
    def get_ffprobe_path(cls) -> Optional[str]:
        return cls._find_binary("ffprobe")

    @classmethod
    def validate_tools(cls) -> Tuple[bool, str]:
        ffmpeg = cls.get_ffmpeg_path()
        ffprobe = cls.get_ffprobe_path()
        missing = []
        if not ffmpeg:
            missing.append("FFmpeg")
        if not ffprobe:
            missing.append("FFprobe")
        if missing:
            return (
                False,
                "Không tìm thấy: " + ", ".join(missing) + ".\n\n"
                "Hãy cài FFmpeg đầy đủ và bảo đảm ffmpeg + ffprobe nằm trong PATH "
                "hoặc đặt ffmpeg.exe / ffprobe.exe cạnh file chương trình.",
            )
        return True, f"FFmpeg: {ffmpeg}\nFFprobe: {ffprobe}"

    @classmethod
    def probe(cls, source_path: Path) -> AudioProbe:
        ffprobe = cls.get_ffprobe_path()
        if not ffprobe:
            raise FileNotFoundError("Không tìm thấy FFprobe.")
        if not source_path.exists():
            raise FileNotFoundError(f"File nguồn không còn tồn tại:\n{source_path}")
        cmd = [
            ffprobe, "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(source_path),
        ]
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        if result.returncode != 0:
            error = result.stderr.strip() or "FFprobe không cung cấp chi tiết lỗi."
            raise RuntimeError(f"FFprobe không đọc được file.\n\n{error}")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("FFprobe trả về dữ liệu không hợp lệ.") from exc
        streams = data.get("streams", [])
        format_info = data.get("format", {})
        audio_stream = None
        for stream in streams:
            if stream.get("codec_type") == "audio":
                audio_stream = stream
                break
        if not audio_stream:
            raise RuntimeError("File không chứa audio stream.")
        cover_stream_index = None
        for stream in streams:
            if stream.get("codec_type") != "video":
                continue
            disposition = stream.get("disposition") or {}
            if int(disposition.get("attached_pic", 0) or 0) == 1:
                try:
                    cover_stream_index = int(stream.get("index"))
                except (TypeError, ValueError):
                    cover_stream_index = None
                break
        return AudioProbe(
            stream=audio_stream,
            format_info=format_info,
            cover_stream_index=cover_stream_index,
        )

    @staticmethod
    def _wav_codec_from_source(probe: AudioProbe) -> str:
        sample_fmt = probe.sample_format
        if sample_fmt in {"flt", "fltp"}:
            return "pcm_f32le"
        if sample_fmt in {"dbl", "dblp"}:
            return "pcm_f64le"
        bits = max(probe.bits_per_raw_sample, probe.bits_per_sample)
        if bits >= 32:
            return "pcm_s32le"
        if bits >= 24:
            return "pcm_s24le"
        if sample_fmt in {"s32", "s32p"} and bits == 0:
            return "pcm_s32le"
        return "pcm_s16le"

    @classmethod
    def build_command(cls, task, profile, temp_output_path, probe, include_artwork=True):
        ffmpeg = cls.get_ffmpeg_path()
        if not ffmpeg:
            raise FileNotFoundError("Không tìm thấy FFmpeg.")
        fmt = profile.format_lower
        cmd = [
            ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning",
            "-progress", "pipe:1", "-y",
            "-i", str(task.source_path),
            "-map", "0:a:0", "-map_metadata", "0", "-map_chapters", "-1",
        ]
        artwork_supported = fmt in {"mp3", "flac", "m4a", "alac"}
        if include_artwork and artwork_supported and probe.cover_stream_index is not None:
            cmd.extend([
                "-map", f"0:{probe.cover_stream_index}",
                "-c:v", "copy", "-disposition:v:0", "attached_pic",
            ])
        else:
            cmd.append("-vn")
        bitrate_kbps = parse_bitrate_kbps(profile.bitrate)
        if fmt == "mp3":
            if probe.channels > 2:
                raise RuntimeError(
                    "Nguồn có hơn 2 kênh.\n\n"
                    "MP3 không phù hợp để giữ nguyên multichannel. "
                    "Bản này không tự ý downmix để tránh làm thay đổi nguồn."
                )
            cmd.extend(["-c:a", "libmp3lame"])
            if bitrate_kbps is None:
                cmd.extend(["-q:a", "0"])
            else:
                cmd.extend(["-b:a", f"{bitrate_kbps}k"])
            cmd.extend(["-id3v2_version", "3", "-write_xing", "1"])
        elif fmt == "flac":
            cmd.extend(["-c:a", "flac", "-compression_level", "8"])
        elif fmt in {"aac", "m4a"}:
            cmd.extend(["-c:a", "aac"])
            if bitrate_kbps is None:
                cmd.extend(["-q:a", "2"])
            else:
                cmd.extend(["-b:a", f"{bitrate_kbps}k"])
            if fmt == "m4a":
                cmd.extend(["-movflags", "+faststart"])
        elif fmt == "alac":
            cmd.extend(["-c:a", "alac", "-movflags", "+faststart"])
        elif fmt == "wav":
            cmd.extend(["-c:a", cls._wav_codec_from_source(probe)])
        elif fmt == "ogg":
            cmd.extend(["-c:a", "libvorbis"])
            if bitrate_kbps is None:
                cmd.extend(["-q:a", "5"])
            else:
                cmd.extend(["-b:a", f"{bitrate_kbps}k"])
        else:
            raise ValueError(f"Định dạng output không được hỗ trợ: {profile.target_format}")
        if profile.sample_rate != "Auto":
            try:
                rate = int(profile.sample_rate.split()[0])
            except (ValueError, IndexError) as exc:
                raise ValueError(f"Sample rate không hợp lệ: {profile.sample_rate!r}") from exc
            if fmt == "mp3" and rate not in {44100, 48000}:
                raise ValueError("MP3 chỉ hỗ trợ preset 44100 Hz hoặc 48000 Hz.")
            cmd.extend(["-ar", str(rate)])
        cmd.append(str(temp_output_path))
        return cmd

    @classmethod
    def run_ffmpeg(cls, task, cmd, cancel_event, set_process, progress_callback):
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=creationflags,
        )
        set_process(process)
        stderr_lines: List[str] = []

        def read_stderr():
            try:
                assert process.stderr is not None
                for line in process.stderr:
                    line = line.strip()
                    if line:
                        stderr_lines.append(line)
                        if len(stderr_lines) > 200:
                            del stderr_lines[:-200]
            except Exception:
                pass

        stderr_thread = threading.Thread(
            target=read_stderr, name=f"stderr-reader-{task.task_id[:8]}", daemon=True,
        )
        stderr_thread.start()
        last_progress = -1.0
        cancelled = False

        def _emit_progress(seconds_value: float):
            nonlocal last_progress
            duration = getattr(task, "_source_duration", 0.0)
            if duration <= 0:
                return
            ratio = max(0.0, min(1.0, seconds_value / duration))
            if abs(ratio - last_progress) >= 0.002:
                last_progress = ratio
                progress_callback(ratio)

        try:
            assert process.stdout is not None
            while True:
                line = process.stdout.readline()
                if line == "":
                    if process.poll() is not None:
                        break
                    continue
                line = line.strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in {"out_time_us", "out_time_ms"}:
                    try:
                        out_time_us = int(value)
                    except (TypeError, ValueError):
                        continue
                    _emit_progress(out_time_us / 1_000_000.0)
                elif key == "progress":
                    if value == "end":
                        progress_callback(1.0)
            returncode = process.wait()
            if cancel_event.is_set():
                cancelled = True
        finally:
            try:
                stderr_thread.join(timeout=2.0)
            except Exception:
                pass
            set_process(None)
        return returncode, stderr_lines, cancelled

    @classmethod
    def verify_output(cls, output_path, profile, source_probe):
        if not output_path.exists():
            return False, "File output không tồn tại."
        try:
            size = output_path.stat().st_size
        except OSError as exc:
            return False, f"Không đọc được dung lượng output: {exc}"
        if size <= 0:
            return False, "File output có kích thước 0 byte."
        try:
            output_probe = cls.probe(output_path)
        except Exception as exc:
            return False, f"FFprobe không verify được output:\n{exc}"
        expected_codec = {
            "mp3": "mp3", "flac": "flac", "aac": "aac", "m4a": "aac",
            "alac": "alac", "wav": cls._wav_codec_from_source(source_probe),
            "ogg": "vorbis",
        }.get(profile.format_lower)
        if expected_codec and output_probe.codec != expected_codec:
            return (
                False,
                f"Codec output không đúng.\nExpected: {expected_codec}\nActual: {output_probe.codec}",
            )
        source_duration = source_probe.duration
        output_duration = output_probe.duration
        if source_duration > 0 and output_duration > 0:
            abs_tolerance = 0.15
            lower = source_duration * 0.95 - abs_tolerance
            upper = source_duration * 1.08 + abs_tolerance
            if not (lower <= output_duration <= upper):
                return (
                    False,
                    "Duration output bất thường.\n"
                    f"Source: {source_duration:.3f}s\nOutput: {output_duration:.3f}s",
                )
        if source_probe.channels > 0 and output_probe.channels > 0:
            if profile.format_lower != "mp3" and source_probe.channels != output_probe.channels:
                return (
                    False,
                    "Số kênh audio thay đổi ngoài dự kiến.\n"
                    f"Source: {source_probe.channels}\nOutput: {output_probe.channels}",
                )
        return True, "Output verified successfully."


# ==============================================================================
# CONDITIONAL BASE CLASS FOR DRAG & DROP
# ==============================================================================

if HAS_DND:
    class _AppBase(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            try:
                self.TkdndVersion = TkinterDnD._require(self)
            except Exception as exc:
                print(f"[dnd] tkdnd load failed: {exc}", file=sys.stderr)
                self.TkdndVersion = None
else:
    class _AppBase(ctk.CTk):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = None


# ==============================================================================
# FILE ROW WIDGET — card cho từng file
# ==============================================================================

class FileRow(ctk.CTkFrame):
    """
    Mỗi file là 1 card. Bố cục:

        [✓]  tên_file.mp3                        8.5 MB  ▬▬▬▬ 100%   ● Completed   ✕
             C:\\Music\\Album
    """

    _STATUS_STYLE = {
        "Pending":       ("#8b949e", "#282e39"),
        "Probing...":    ("#4f8cff", "#152744"),
        "Converting...": ("#4f8cff", "#152744"),
        "Verifying...":  ("#4f8cff", "#152744"),
        "Completed":     ("#2ea043", "#0f2a17"),
        "Failed":        ("#f85149", "#2c1414"),
        "Skipped":       ("#8b949e", "#282e39"),
        "Cancelled":     ("#8b949e", "#282e39"),
    }

    def __init__(self, parent, task: ConversionTask, app):
        super().__init__(
            parent,
            fg_color=app._C_SURFACE_2,
            corner_radius=10,
            border_width=1,
            border_color=app._C_BORDER,
        )
        self.app = app
        self.task = task
        self._hover = False
        self._leave_job = None

        self.grid_columnconfigure(1, weight=1)

        # --- Checkbox ---
        self.var_enabled = tk.BooleanVar(value=task.enabled)
        self.chk = ctk.CTkCheckBox(
            self,
            text="",
            width=26,
            checkbox_width=22,
            checkbox_height=22,
            variable=self.var_enabled,
            command=self._on_check_change,
            fg_color=app._C_ACCENT,
            hover_color=app._C_ACCENT_HOVER,
            border_color=app._C_MUTED,
            checkmark_color="#ffffff",
            corner_radius=6,
        )
        self.chk.grid(row=0, column=0, rowspan=2, padx=(16, 14), pady=14)

        # --- Name ---
        self.lbl_name = ctk.CTkLabel(
            self,
            text=task.source_path.name,
            anchor="w",
            font=("Segoe UI Semibold", 13),
            text_color=app._C_TEXT,
        )
        self.lbl_name.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=(12, 0))

        # --- Path ---
        self.lbl_path = ctk.CTkLabel(
            self,
            text=str(task.source_path.parent),
            anchor="w",
            font=("Segoe UI", 10),
            text_color=app._C_MUTED,
        )
        self.lbl_path.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=(0, 12))

        # --- Size ---
        try:
            size_text = app._format_size(task.source_path.stat().st_size)
        except Exception:
            size_text = "?"
        self.lbl_size = ctk.CTkLabel(
            self,
            text=size_text,
            font=("Segoe UI", 11),
            text_color=app._C_MUTED,
            width=80,
            anchor="e",
        )
        self.lbl_size.grid(row=0, column=2, rowspan=2, padx=(0, 16))

        # --- Progress bar ---
        self.progress = ctk.CTkProgressBar(
            self,
            width=180,
            height=8,
            corner_radius=4,
            progress_color=app._C_ACCENT,
            fg_color=app._C_SURFACE_3,
        )
        self.progress.set(0)
        self.progress.grid(row=0, column=3, rowspan=2, padx=(0, 12))

        # --- Percent ---
        self.lbl_pct = ctk.CTkLabel(
            self,
            text="0%",
            font=("Segoe UI", 11),
            text_color=app._C_MUTED,
            width=42,
            anchor="e",
        )
        self.lbl_pct.grid(row=0, column=4, rowspan=2, padx=(0, 14))

        # --- Status badge ---
        self.lbl_status = ctk.CTkLabel(
            self,
            text="Pending",
            font=("Segoe UI Semibold", 10),
            text_color=app._C_MUTED,
            fg_color=app._C_SURFACE_3,
            corner_radius=11,
            height=24,
            width=110,
        )
        self.lbl_status.grid(row=0, column=5, rowspan=2, padx=(0, 10))

        # --- Remove button ---
        self.btn_remove = ctk.CTkButton(
            self,
            text="✕",
            width=30, height=30,
            fg_color="transparent",
            hover_color=app._C_DANGER,
            text_color=app._C_MUTED,
            font=("Segoe UI Semibold", 14),
            corner_radius=8,
            command=self._on_remove,
        )
        self.btn_remove.grid(row=0, column=6, rowspan=2, padx=(0, 12))

        self._bind_interactions()
        self._apply_enabled_style()

    # ---------- Hover + click handlers ----------

    def _bind_interactions(self):
        surfaces = [
            self,
            self.lbl_name,
            self.lbl_path,
            self.lbl_size,
            self.lbl_pct,
            self.lbl_status,
        ]
        try:
            if hasattr(self, "_canvas"):
                surfaces.append(self._canvas)
        except Exception:
            pass

        for w in surfaces:
            try:
                w.bind("<Button-1>", self._on_row_click, add="+")
                w.bind("<Enter>", self._on_enter, add="+")
                w.bind("<Leave>", self._on_leave, add="+")
                w.bind("<Button-3>", self._on_right_click, add="+")
                w.bind("<Button-2>", self._on_right_click, add="+")
            except Exception:
                pass

    def _on_enter(self, _event=None):
        if self._leave_job is not None:
            try:
                self.after_cancel(self._leave_job)
            except Exception:
                pass
            self._leave_job = None
        if self._hover:
            return
        self._hover = True
        try:
            self.configure(
                fg_color=self.app._C_SURFACE_3,
                border_color=self.app._C_ACCENT,
            )
        except Exception:
            pass

    def _on_leave(self, _event=None):
        if self._leave_job is not None:
            try:
                self.after_cancel(self._leave_job)
            except Exception:
                pass
        try:
            self._leave_job = self.after(80, self._do_leave)
        except Exception:
            self._do_leave()

    def _do_leave(self):
        self._leave_job = None
        self._hover = False
        try:
            self.configure(
                fg_color=self.app._C_SURFACE_2,
                border_color=self.app._C_BORDER,
            )
        except Exception:
            pass

    def _on_row_click(self, event):
        if self.app.is_converting or self.app.scan_in_progress:
            return
        try:
            if event.widget is self.chk:
                return
        except Exception:
            pass
        try:
            self.var_enabled.set(not self.var_enabled.get())
            self._on_check_change()
        except Exception:
            pass

    def _on_right_click(self, event):
        if self.app.is_converting or self.app.scan_in_progress:
            return
        try:
            menu = tk.Menu(
                self, tearoff=0,
                bg=self.app._C_SURFACE_2, fg=self.app._C_TEXT,
                activebackground=self.app._C_ACCENT, activeforeground="#ffffff",
                font=("Segoe UI", 11),
                borderwidth=0, relief="flat",
            )
            label = "☐  Tắt file này" if self.task.enabled else "✓  Bật file này"
            menu.add_command(label=label, command=self._toggle_enabled)
            menu.add_separator()
            menu.add_command(label="📂  Mở file nguồn", command=self._open_file)
            menu.add_command(label="📁  Mở thư mục chứa", command=self._open_folder)
            menu.add_separator()
            menu.add_command(label="🗑  Xoá khỏi danh sách", command=self._on_remove)
            menu.tk_popup(event.x_root, event.y_root)
        except Exception:
            pass
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    # ---------- Helpers ----------

    def _toggle_enabled(self):
        if self.app.is_converting or self.app.scan_in_progress:
            return
        self.var_enabled.set(not self.var_enabled.get())
        self._on_check_change()

    def _on_check_change(self):
        self.task.enabled = bool(self.var_enabled.get())
        self._apply_enabled_style()
        self.app._refresh_selection_count()

    def set_enabled(self, enabled: bool):
        self.task.enabled = enabled
        self.var_enabled.set(enabled)
        self._apply_enabled_style()

    def _apply_enabled_style(self):
        if self.task.enabled:
            self.lbl_name.configure(text_color=self.app._C_TEXT)
            self.lbl_path.configure(text_color=self.app._C_MUTED)
        else:
            self.lbl_name.configure(text_color=self.app._C_ROW_DIM)
            self.lbl_path.configure(text_color=self.app._C_ROW_DIM)

    def update_status(self, status: str):
        self.task.status = status
        fg, bg = self._STATUS_STYLE.get(status, ("#8b949e", "#282e39"))
        try:
            self.lbl_status.configure(text=status, text_color=fg, fg_color=bg)
        except Exception:
            pass

    def update_progress(self, p: float):
        self.task.progress = p
        try:
            self.progress.set(p)
            self.lbl_pct.configure(text=f"{p * 100:.0f}%")
        except Exception:
            pass

    def _on_remove(self):
        self.app._remove_row(self.task.task_id)

    def _open_file(self):
        try:
            if os.name == "nt":
                os.startfile(str(self.task.source_path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.task.source_path)])
            else:
                subprocess.Popen(["xdg-open", str(self.task.source_path)])
        except Exception:
            pass

    def _open_folder(self):
        folder = self.task.source_path.parent
        try:
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception:
            pass


# ==============================================================================
# MAIN APPLICATION
# ==============================================================================

class AudioConverterApp(_AppBase):

    def __init__(self):
        super().__init__()

        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.geometry(DEFAULT_SETTINGS["geometry"])
        self.minsize(1040, 720)

        self.tasks: Dict[str, ConversionTask] = {}
        self.row_widgets: Dict[str, FileRow] = {}
        self.source_index = set()
        self.ui_queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.scan_cancel_event = threading.Event()
        self.process_lock = threading.Lock()
        self.current_process: Optional[subprocess.Popen] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.scan_thread: Optional[threading.Thread] = None
        self.is_converting = False
        self.closing = False
        self.scan_in_progress = False
        self._drag_highlight_active = False
        self.full_logs: List[str] = []

        self.completed_count = 0
        self.failed_count = 0
        self.skipped_count = 0
        self.cancelled_count = 0

        self.settings = self._load_settings()
        self._setup_ui()
        self._apply_settings()
        self._setup_drag_drop()

        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.after(50, self._process_ui_queue)

    # ----------------------------------------------------------------------
    # Drag & drop
    # ----------------------------------------------------------------------

    def _setup_drag_drop(self):
        if not HAS_DND:
            return
        if getattr(self, "TkdndVersion", None) is None:
            return
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
            self.dnd_bind("<<DragEnter>>", self._on_drag_enter)
            self.dnd_bind("<<DragLeave>>", self._on_drag_leave)
        except Exception as exc:
            print(f"[dnd] register failed: {exc}", file=sys.stderr)

    def _set_drag_highlight(self, active: bool):
        if active == self._drag_highlight_active:
            return
        try:
            if active:
                self.frame_list.configure(border_color=_DND_HIGHLIGHT_COLOR, border_width=2)
            else:
                self.frame_list.configure(border_width=0)
            self._drag_highlight_active = active
        except Exception:
            pass

    def _on_drag_enter(self, _event):
        if self.is_converting or self.scan_in_progress:
            return
        self._set_drag_highlight(True)

    def _on_drag_leave(self, _event):
        self._set_drag_highlight(False)

    def _on_drop(self, event):
        self._set_drag_highlight(False)
        if self.is_converting:
            self._append_log("Drag & drop bị bỏ qua: đang convert.")
            return
        if self.scan_in_progress:
            self._append_log("Drag & drop bị bỏ qua: đang quét thư mục.")
            return
        try:
            raw_paths = self.tk.splitlist(event.data)
        except Exception as exc:
            print(f"[dnd] splitlist failed: {exc}", file=sys.stderr)
            return
        if not raw_paths:
            return
        files_to_add: List[Path] = []
        folders_to_scan: List[Path] = []
        rejected: List[str] = []
        for raw in raw_paths:
            if not raw:
                continue
            try:
                p = Path(raw)
            except (OSError, ValueError):
                continue
            try:
                if p.is_dir():
                    folders_to_scan.append(p)
                    continue
                if p.is_file():
                    if p.suffix.lower() in INPUT_EXTENSIONS:
                        files_to_add.append(p)
                    else:
                        rejected.append(p.name)
                    continue
            except OSError:
                continue
        added = 0
        for f in files_to_add:
            if self._add_path(f):
                added += 1
        if added > 0:
            self._set_status(f"Drag & drop · Added {added} file(s) · Total: {len(self.tasks)}")
            self._append_log(f"Drag & drop: added {added} file(s).")
        if rejected:
            preview = ", ".join(rejected[:5])
            suffix = "" if len(rejected) <= 5 else f" (+{len(rejected) - 5} more)"
            self._append_log(
                f"Drag & drop: bỏ qua {len(rejected)} file không phải audio: {preview}{suffix}"
            )
        if folders_to_scan:
            self._start_drop_scan(folders_to_scan)

    def _start_drop_scan(self, folders: List[Path]):
        self.scan_in_progress = True
        self.scan_cancel_event.clear()
        self.btn_add.configure(state="disabled")
        self.btn_add_folder.configure(state="disabled")
        self.btn_convert.configure(state="disabled")
        self._refresh_cancel_button()
        if len(folders) == 1:
            self._set_status(f"Scanning folder (drag & drop): {folders[0]} ...")
        else:
            self._set_status(f"Scanning {len(folders)} folders (drag & drop) ...")
        self.scan_thread = threading.Thread(
            target=self._scan_folders_worker,
            args=(folders, self.scan_cancel_event),
            name="DropFolderScanner", daemon=True,
        )
        self.scan_thread.start()

    def _scan_folders_worker(self, folders, cancel_event):
        paths: List[Path] = []
        failed_folders: List[Tuple[Path, str]] = []
        was_cancelled = False
        scan_failed = False
        error_msg = ""
        try:
            for folder in folders:
                if cancel_event.is_set():
                    was_cancelled = True
                    break
                try:
                    for path in folder.rglob("*"):
                        if cancel_event.is_set():
                            was_cancelled = True
                            break
                        try:
                            if path.is_file() and path.suffix.lower() in INPUT_EXTENSIONS:
                                paths.append(path)
                        except OSError:
                            continue
                except OSError as exc:
                    failed_folders.append((folder, str(exc)))
                    continue
                if was_cancelled:
                    break
        except Exception as exc:
            scan_failed = True
            error_msg = str(exc)
        finally:
            if scan_failed:
                self.ui_queue.put(("SCAN_ERROR", error_msg))
            else:
                for folder, err in failed_folders[:5]:
                    self.ui_queue.put(("LOG", f"Cảnh báo: không quét được thư mục {folder}: {err}"))
                if len(failed_folders) > 5:
                    self.ui_queue.put(("LOG", f"... và {len(failed_folders) - 5} thư mục khác cũng bị lỗi."))
                self.ui_queue.put(("BULK_ADD_PATHS", (paths, was_cancelled)))

    # ----------------------------------------------------------------------
    # Settings
    # ----------------------------------------------------------------------

    def _load_settings(self) -> Dict:
        settings = dict(DEFAULT_SETTINGS)
        try:
            if SETTINGS_FILE.exists():
                with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
        except Exception as exc:
            print(f"[settings] load failed: {exc}", file=sys.stderr)
        if settings.get("format") not in SUPPORTED_FORMATS:
            settings["format"] = DEFAULT_SETTINGS["format"]
        bitrate_value = settings.get("bitrate")
        if bitrate_value not in BITRATES and bitrate_value != "N/A (Lossless)":
            settings["bitrate"] = DEFAULT_SETTINGS["bitrate"]
        if settings.get("sample_rate") not in SAMPLE_RATES_ALL:
            settings["sample_rate"] = DEFAULT_SETTINGS["sample_rate"]
        if settings.get("conflict_policy") not in CONFLICT_POLICIES:
            settings["conflict_policy"] = DEFAULT_SETTINGS["conflict_policy"]
        output_dir = settings.get("output_dir")
        if not isinstance(output_dir, str) or not output_dir.strip():
            settings["output_dir"] = DEFAULT_SETTINGS["output_dir"]
        geometry = settings.get("geometry")
        if not isinstance(geometry, str) or "x" not in geometry:
            settings["geometry"] = DEFAULT_SETTINGS["geometry"]
        return settings

    def _save_settings(self):
        try:
            data = {
                "format": self.combo_format.get(),
                "bitrate": self.combo_bitrate.get(),
                "sample_rate": self.combo_samplerate.get(),
                "output_dir": self.entry_output.get().strip(),
                "conflict_policy": self.combo_conflict.get(),
                "geometry": self.geometry(),
            }
            with SETTINGS_FILE.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"[settings] save failed: {exc}", file=sys.stderr)

    def _apply_settings(self):
        self.combo_format.set(self.settings.get("format", "MP3"))
        self.combo_bitrate.set(self.settings.get("bitrate", "Auto"))
        self.combo_samplerate.set(self.settings.get("sample_rate", "Auto"))
        self.combo_conflict.set(self.settings.get("conflict_policy", "Rename"))
        output_dir = self.settings.get("output_dir", str(Path.home() / "Music"))
        self.entry_output.delete(0, tk.END)
        self.entry_output.insert(0, output_dir)
        self._update_option_states()
        saved_geometry = self.settings.get("geometry", DEFAULT_SETTINGS["geometry"])
        try:
            self.geometry(saved_geometry)
        except Exception:
            pass

    # ----------------------------------------------------------------------
    # UI construction
    # ----------------------------------------------------------------------

    def _setup_ui(self):
        ctk.set_appearance_mode("dark")

        self._C_BG           = "#0d1117"
        self._C_SURFACE      = "#161b22"
        self._C_SURFACE_2    = "#1f242d"
        self._C_SURFACE_3    = "#282e39"
        self._C_BORDER       = "#30363d"
        self._C_TEXT         = "#e6edf3"
        self._C_MUTED        = "#8b949e"
        self._C_ACCENT       = "#4f8cff"
        self._C_ACCENT_HOVER = "#3d7aee"
        self._C_SUCCESS      = "#2ea043"
        self._C_SUCCESS_HOV  = "#238636"
        self._C_DANGER       = "#da3633"
        self._C_DANGER_HOV   = "#b62324"
        self._C_ROW_DIM      = "#5a6169"

        self.configure(fg_color=self._C_BG)

        PADX = 22
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ---------- HEADER ----------
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=PADX, pady=(18, 12), sticky="ew")
        header.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(
            header, text="🎧",
            font=("Segoe UI Emoji", 30),
        ).grid(row=0, column=0, padx=(0, 14), sticky="w")

        ctk.CTkLabel(
            header, text="Audio Converter Pro",
            font=("Segoe UI Semibold", 22),
            text_color=self._C_TEXT,
        ).grid(row=0, column=1, sticky="w")

        ctk.CTkLabel(
            header, text=f"  v{APP_VERSION}  ",
            font=("Segoe UI", 10),
            text_color=self._C_ACCENT, fg_color="#1a2b4a",
            corner_radius=9, height=22,
        ).grid(row=0, column=2, padx=(12, 0), sticky="w")

        # ---------- OPTIONS CARD ----------
        self.frame_options = ctk.CTkFrame(
            self, fg_color=self._C_SURFACE,
            corner_radius=14, border_width=1, border_color=self._C_BORDER,
        )
        self.frame_options.grid(row=1, column=0, padx=PADX, pady=(0, 10), sticky="ew")
        self.frame_options.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(self.frame_options, fg_color="transparent")
        inner.grid(row=0, column=0, padx=18, pady=(16, 6), sticky="ew")

        def _make_field(parent, col, label_text, values, width):
            f = ctk.CTkFrame(parent, fg_color="transparent")
            f.grid(row=0, column=col, padx=(0, 16), sticky="w")
            ctk.CTkLabel(
                f, text=label_text,
                font=("Segoe UI Semibold", 10), text_color=self._C_MUTED,
            ).pack(anchor="w", pady=(0, 5))
            combo = ctk.CTkComboBox(
                f, values=values, width=width,
                fg_color=self._C_SURFACE_2, border_color=self._C_BORDER,
                button_color=self._C_SURFACE_2, button_hover_color=self._C_ACCENT,
                dropdown_fg_color=self._C_SURFACE_2,
                dropdown_hover_color=self._C_ACCENT,
                dropdown_text_color=self._C_TEXT,
                text_color=self._C_TEXT, font=("Segoe UI", 12),
                corner_radius=8, height=34, border_width=1,
            )
            combo.pack(anchor="w")
            return combo

        self.combo_format = _make_field(inner, 0, "ĐỊNH DẠNG", SUPPORTED_FORMATS, 130)
        self.combo_format.configure(command=self._on_format_change)
        self.combo_bitrate = _make_field(inner, 1, "BITRATE", BITRATES, 140)
        self.combo_samplerate = _make_field(inner, 2, "SAMPLE RATE", SAMPLE_RATES_ALL, 140)
        self.combo_conflict = _make_field(inner, 3, "FILE ĐÃ TỒN TẠI", CONFLICT_POLICIES, 140)

        info_bar = ctk.CTkFrame(self.frame_options, fg_color="transparent")
        info_bar.grid(row=1, column=0, padx=18, pady=(0, 14), sticky="ew")
        info_bar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            info_bar, text="ℹ", font=("Segoe UI Emoji", 13),
            text_color=self._C_ACCENT,
        ).grid(row=0, column=0, padx=(0, 6), sticky="w")
        self.lbl_preset_info = ctk.CTkLabel(
            info_bar,
            text="Auto: format-specific VBR / lossless preset",
            anchor="w", font=("Segoe UI", 11), text_color=self._C_MUTED,
        )
        self.lbl_preset_info.grid(row=0, column=1, sticky="ew")

        # ---------- FILES CARD ----------
        self.frame_list = ctk.CTkFrame(
            self, fg_color=self._C_SURFACE,
            corner_radius=14, border_width=0, border_color=self._C_BORDER,
        )
        self.frame_list.grid(row=2, column=0, padx=PADX, pady=(0, 10), sticky="nsew")
        self.frame_list.grid_columnconfigure(0, weight=1)
        self.frame_list.grid_rowconfigure(2, weight=1) # List scroll is row 2 now

        # List Header (Row 0)
        list_header = ctk.CTkFrame(self.frame_list, fg_color="transparent")
        list_header.grid(row=0, column=0, padx=18, pady=(14, 8), sticky="ew")
        list_header.grid_columnconfigure(1, weight=1)
        
        ctk.CTkLabel(
            list_header, text="📄  DANH SÁCH FILE", 
            font=("Segoe UI Semibold", 11), text_color=self._C_TEXT
        ).grid(row=0, column=0, sticky="w")
        
        self.lbl_selection = ctk.CTkLabel(
            list_header, text="", anchor="e",
            font=("Segoe UI Semibold", 11), text_color=self._C_MUTED,
        )
        self.lbl_selection.grid(row=0, column=1, sticky="e")

        # Toolbar (Row 1)
        toolbar = ctk.CTkFrame(self.frame_list, fg_color="transparent")
        toolbar.grid(row=1, column=0, padx=14, pady=(0, 8), sticky="ew")
        toolbar.grid_columnconfigure(6, weight=1) # Spacer column

        self.btn_add = ctk.CTkButton(
            toolbar, text="➕  Thêm Files", width=120, height=32,
            fg_color=self._C_ACCENT, hover_color=self._C_ACCENT_HOVER,
            font=("Segoe UI Semibold", 12), corner_radius=8,
            command=self.add_files,
        )
        self.btn_add.grid(row=0, column=0, padx=(0, 6))

        self.btn_add_folder = ctk.CTkButton(
            toolbar, text="📁  Thêm Folder", width=130, height=32,
            fg_color=self._C_SURFACE_2, hover_color=self._C_SURFACE_3,
            text_color=self._C_TEXT, font=("Segoe UI Semibold", 12),
            corner_radius=8, border_width=1, border_color=self._C_BORDER,
            command=self.add_folder,
        )
        self.btn_add_folder.grid(row=0, column=1, padx=3)

        sep1 = ctk.CTkFrame(toolbar, width=1, height=22, fg_color=self._C_BORDER)
        sep1.grid(row=0, column=2, padx=8)

        self.btn_enable_all = ctk.CTkButton(
            toolbar, text="✓  Bật hết", width=90, height=32,
            fg_color=self._C_SURFACE_2, hover_color=self._C_SURFACE_3,
            text_color=self._C_TEXT, font=("Segoe UI Semibold", 12),
            corner_radius=8, border_width=1, border_color=self._C_BORDER,
            command=lambda: self._set_all_enabled(True),
        )
        self.btn_enable_all.grid(row=0, column=3, padx=3)

        self.btn_disable_all = ctk.CTkButton(
            toolbar, text="☐  Tắt hết", width=90, height=32,
            fg_color=self._C_SURFACE_2, hover_color=self._C_SURFACE_3,
            text_color=self._C_TEXT, font=("Segoe UI Semibold", 12),
            corner_radius=8, border_width=1, border_color=self._C_BORDER,
            command=lambda: self._set_all_enabled(False),
        )
        self.btn_disable_all.grid(row=0, column=4, padx=3)

        self.btn_clear = ctk.CTkButton(
            toolbar, text="🗑  Xoá hết", width=100, height=32,
            fg_color="transparent", hover_color=self._C_SURFACE_2,
            text_color=self._C_MUTED, font=("Segoe UI Semibold", 12),
            corner_radius=8, border_width=1, border_color=self._C_BORDER,
            command=self.clear_all_files,
        )
        self.btn_clear.grid(row=0, column=5, padx=3)

        # Moved Convert and Cancel buttons to the toolbar (Right aligned)
        self.btn_cancel = ctk.CTkButton(
            toolbar, text="■  Huỷ", width=90, height=32,
            fg_color=self._C_DANGER, hover_color=self._C_DANGER_HOV,
            font=("Segoe UI Semibold", 12), corner_radius=8,
            state="disabled", command=self.cancel_conversion,
        )
        self.btn_cancel.grid(row=0, column=7, padx=(6, 6))

        self.btn_convert = ctk.CTkButton(
            toolbar, text="▶  Bắt đầu Convert", width=160, height=32,
            fg_color=self._C_SUCCESS, hover_color=self._C_SUCCESS_HOV,
            font=("Segoe UI Semibold", 12), corner_radius=8,
            command=self.start_conversion,
        )
        self.btn_convert.grid(row=0, column=8, padx=(0, 0))

        # Scrollable list (Row 2)
        self.list_scroll = ctk.CTkScrollableFrame(
            self.frame_list,
            fg_color=self._C_SURFACE,
            scrollbar_button_color=self._C_SURFACE_3,
            scrollbar_button_hover_color=self._C_MUTED,
        )
        self.list_scroll.grid(row=2, column=0, padx=10, pady=(0, 12), sticky="nsew")
        self.list_scroll.grid_columnconfigure(0, weight=1)

        # Empty-state hint — Căn giữa hoàn hảo
        self.lbl_empty = ctk.CTkLabel(
            self.list_scroll,
            text="🎵\n\nChưa có file nào trong danh sách\n\nKéo & thả file video hoặc thư mục vào đây\nhoặc bấm 'Thêm File' / 'Thêm Thư Mục'.",
            font=("Segoe UI", 13),
            text_color=self._C_MUTED,
            justify="center",
            anchor="center",
        )
        self.lbl_empty.pack(fill="x", pady=(60, 0))

        # ---------- BOTTOM PANEL (Output + Log + Progress) ----------
        self.bottom_panel = ctk.CTkFrame(self, fg_color="transparent")
        self.bottom_panel.grid(row=3, column=0, padx=PADX, pady=(0, 18), sticky="ew")
        self.bottom_panel.grid_columnconfigure(0, weight=1)

        # Output Row (Row 0)
        out_row = ctk.CTkFrame(self.bottom_panel, fg_color="transparent")
        out_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        out_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            out_row, text="Thư mục xuất:", 
            font=("Segoe UI Semibold", 12), text_color=self._C_TEXT
        ).grid(row=0, column=0, padx=(0, 10), sticky="w")

        self.entry_output = ctk.CTkEntry(
            out_row,
            fg_color=self._C_SURFACE_2, border_color=self._C_BORDER,
            text_color=self._C_TEXT, font=("Segoe UI", 12),
            corner_radius=8, height=34,
            placeholder_text="Chọn thư mục output...",
        )
        self.entry_output.grid(row=0, column=1, padx=(0, 10), sticky="ew")

        self.btn_browse = ctk.CTkButton(
            out_row, text="Chọn Thư Mục...", width=130, height=34,
            fg_color=self._C_SURFACE_2, hover_color=self._C_SURFACE_3,
            text_color=self._C_TEXT, font=("Segoe UI Semibold", 12),
            corner_radius=8, border_width=1, border_color=self._C_BORDER,
            command=self.browse_output_dir,
        )
        self.btn_browse.grid(row=0, column=2, padx=(0, 6))

        self.btn_open_output = ctk.CTkButton(
            out_row, text="↗  Mở", width=80, height=34,
            fg_color=self._C_SURFACE_2, hover_color=self._C_SURFACE_3,
            text_color=self._C_TEXT, font=("Segoe UI Semibold", 12),
            corner_radius=8, border_width=1, border_color=self._C_BORDER,
            command=self.open_output_directory,
        )
        self.btn_open_output.grid(row=0, column=3)

        # Log Textbox (Row 1)
        self.txt_log = ctk.CTkTextbox(
            self.bottom_panel,
            height=70,
            fg_color=self._C_SURFACE_2,
            border_color=self._C_BORDER,
            border_width=1,
            corner_radius=10,
            text_color=self._C_MUTED,
            font=("Consolas", 10),
            scrollbar_button_color=self._C_SURFACE_3,
            scrollbar_button_hover_color=self._C_MUTED,
        )
        self.txt_log.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.txt_log.configure(state="disabled")

        # Progress + Status (Row 2 & 3)
        self.progress_bar = ctk.CTkProgressBar(
            self.bottom_panel, height=8, corner_radius=4,
            progress_color=self._C_ACCENT, fg_color=self._C_SURFACE_2,
        )
        self.progress_bar.grid(row=2, column=0, sticky="ew")
        self.progress_bar.set(0)

        self.lbl_status = ctk.CTkLabel(
            self.bottom_panel, text="Sẵn sàng",
            anchor="w", font=("Segoe UI", 11), text_color=self._C_MUTED,
        )
        self.lbl_status.grid(row=3, column=0, pady=(4, 0), sticky="ew")

        self._refresh_selection_count()

    # ----------------------------------------------------------------------
    # Log / status helpers
    # ----------------------------------------------------------------------

    def _append_log(self, text: str):
        line = text.rstrip()
        self.full_logs.append(line)
        if len(self.full_logs) > _FULL_LOG_MAX_LINES:
            del self.full_logs[:_FULL_LOG_TRIM_CHUNK]

        try:
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", line + "\n")
            try:
                current_size = int(self.txt_log.index("end-1c").split(".")[0])
                if current_size > _LOG_VISIBLE_MAX_LINES:
                    self.txt_log.delete("1.0", f"{current_size - _LOG_VISIBLE_TRIM_CHUNK}.0")
            except Exception:
                pass
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        except Exception:
            pass

    def _clear_visible_log(self):
        try:
            self.txt_log.configure(state="normal")
            self.txt_log.delete("1.0", "end")
            self.txt_log.configure(state="disabled")
        except Exception:
            pass

    def _set_status(self, text: str):
        try:
            self.lbl_status.configure(text=text)
        except Exception:
            pass

    def _update_option_states(self):
        fmt = self.combo_format.get().strip().upper()
        if fmt in {"FLAC", "WAV", "ALAC"}:
            self.combo_bitrate.configure(state="disabled")
            self.combo_bitrate.set("N/A (Lossless)")
        else:
            self.combo_bitrate.configure(state="normal")
            if self.combo_bitrate.get() == "N/A (Lossless)":
                self.combo_bitrate.set("Auto")
        if fmt == "MP3":
            current = self.combo_samplerate.get()
            self.combo_samplerate.configure(values=SAMPLE_RATES_MP3)
            if current not in SAMPLE_RATES_MP3:
                self.combo_samplerate.set("Auto")
        else:
            current = self.combo_samplerate.get()
            self.combo_samplerate.configure(values=SAMPLE_RATES_ALL)
            if current not in SAMPLE_RATES_ALL:
                self.combo_samplerate.set("Auto")
        info = {
            "MP3": "Auto = LAME V0",
            "FLAC": "Lossless · compression level 8",
            "AAC": "Auto = AAC VBR",
            "M4A": "AAC VBR trong container M4A",
            "ALAC": "Lossless Apple ALAC",
            "WAV": "PCM · cố giữ bit depth/sample type nguồn",
            "OGG": "Auto = Vorbis VBR quality 5",
        }
        self.lbl_preset_info.configure(text=info.get(fmt, "Auto"))

    def _on_format_change(self, _choice: str):
        self._update_option_states()

    def _refresh_cancel_button(self):
        should_enable = self.is_converting or self.scan_in_progress
        try:
            self.btn_cancel.configure(state="normal" if should_enable else "disabled")
        except Exception:
            pass

    def _set_ui_state(self, converting: bool):
        self.is_converting = converting
        state = "disabled" if converting else "normal"
        widgets = [
            self.btn_add, self.btn_add_folder, self.btn_enable_all,
            self.btn_disable_all, self.btn_clear, self.btn_convert,
            self.btn_browse, self.combo_format, self.combo_samplerate,
            self.combo_conflict, self.btn_open_output,
            self.entry_output,
        ]
        for widget in widgets:
            try:
                widget.configure(state=state)
            except Exception:
                pass
        self._refresh_cancel_button()
        if converting:
            try:
                self.combo_bitrate.configure(state="disabled")
            except Exception:
                pass
        else:
            self._update_option_states()

    @staticmethod
    def _format_size(size: int) -> str:
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(value)} {unit}"
                return f"{value:.2f} {unit}"
            value /= 1024
        return f"{size} B"

    # ----------------------------------------------------------------------
    # Selection counter / empty state
    # ----------------------------------------------------------------------

    def _refresh_selection_count(self):
        total = len(self.tasks)
        enabled = sum(1 for t in self.tasks.values() if t.enabled)
        try:
            if total == 0:
                self.lbl_selection.configure(text="Chưa có file", text_color=self._C_MUTED)
            else:
                self.lbl_selection.configure(
                    text=f"☑  {enabled}/{total} file được bật",
                    text_color=self._C_ACCENT if enabled > 0 else self._C_MUTED,
                )
        except Exception:
            pass
        try:
            if total == 0:
                self.lbl_empty.pack(fill="x", pady=(60, 0))
            else:
                self.lbl_empty.pack_forget()
        except Exception:
            pass

    # ----------------------------------------------------------------------
    # Row operations
    # ----------------------------------------------------------------------

    def _add_path(self, path: Path) -> bool:
        try:
            path = path.resolve()
            if not path.exists() or not path.is_file():
                return False
            key = os.path.normcase(str(path))
            if key in self.source_index:
                return False
            task = ConversionTask(source_path=path)
            self.tasks[task.task_id] = task
            self.source_index.add(key)

            row = FileRow(self.list_scroll, task, self)
            row.pack(fill="x", padx=4, pady=5)
            self.row_widgets[task.task_id] = row

            self._refresh_selection_count()
            return True
        except Exception as exc:
            print(f"[add_path] {exc}", file=sys.stderr)
            return False

    def _remove_row(self, task_id: str):
        if self.is_converting or self.scan_in_progress:
            return
        task = self.tasks.pop(task_id, None)
        if task:
            self.source_index.discard(os.path.normcase(str(task.source_path)))
        row = self.row_widgets.pop(task_id, None)
        if row:
            try:
                row.destroy()
            except Exception:
                pass
        self._refresh_selection_count()
        self._set_status(f"Total files: {len(self.tasks)}")

    def _set_all_enabled(self, enabled: bool):
        for task_id, task in self.tasks.items():
            if task.enabled == enabled:
                continue
            task.enabled = enabled
            row = self.row_widgets.get(task_id)
            if row:
                try:
                    row.set_enabled(enabled)
                except Exception:
                    pass
        self._refresh_selection_count()

    # ----------------------------------------------------------------------
    # File operations
    # ----------------------------------------------------------------------

    def add_files(self):
        files = filedialog.askopenfilenames(
            title="Select Audio Files",
            filetypes=[
                (
                    "Audio Files",
                    "*.mp3 *.flac *.wav *.wave *.m4a *.m4b "
                    "*.aac *.ogg *.oga *.opus *.wma *.wv *.aiff *.aif *.ape",
                ),
                ("All Files", "*.*"),
            ],
        )
        if not files:
            return
        added = sum(1 for f in files if self._add_path(Path(f)))
        self._set_status(f"Added {added} file(s) · Total: {len(self.tasks)}")

    def add_folder(self):
        if self.scan_in_progress or self.is_converting:
            return
        folder = filedialog.askdirectory(title="Select Audio Folder")
        if not folder:
            return
        self.scan_in_progress = True
        self.scan_cancel_event.clear()
        self.btn_add.configure(state="disabled")
        self.btn_add_folder.configure(state="disabled")
        self.btn_convert.configure(state="disabled")
        self._refresh_cancel_button()
        self._set_status(f"Scanning folder: {folder} ...")
        self.scan_thread = threading.Thread(
            target=self._scan_folder_worker,
            args=(Path(folder), self.scan_cancel_event),
            name="FolderScanner", daemon=True,
        )
        self.scan_thread.start()

    def _scan_folder_worker(self, folder_path: Path, cancel_event: threading.Event):
        paths: List[Path] = []
        was_cancelled = False
        scan_failed = False
        error_msg = ""
        try:
            for path in folder_path.rglob("*"):
                if cancel_event.is_set():
                    was_cancelled = True
                    break
                try:
                    if path.is_file() and path.suffix.lower() in INPUT_EXTENSIONS:
                        paths.append(path)
                except OSError:
                    continue
        except Exception as exc:
            scan_failed = True
            error_msg = str(exc)
        finally:
            if scan_failed:
                self.ui_queue.put(("SCAN_ERROR", error_msg))
            else:
                self.ui_queue.put(("BULK_ADD_PATHS", (paths, was_cancelled)))

    def clear_all_files(self):
        if self.is_converting or self.scan_in_progress:
            return
        self.tasks.clear()
        self.source_index.clear()
        for row in self.row_widgets.values():
            try:
                row.destroy()
            except Exception:
                pass
        self.row_widgets.clear()
        self.progress_bar.set(0)
        self._set_status("Ready")
        self._refresh_selection_count()

    def browse_output_dir(self):
        directory = filedialog.askdirectory(title="Select Output Directory")
        if not directory:
            return
        self.entry_output.delete(0, tk.END)
        self.entry_output.insert(0, directory)

    def open_output_directory(self):
        raw = self.entry_output.get().strip()
        if not raw:
            return
        path = Path(raw).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Output Error", f"Không thể truy cập output directory:\n\n{exc}")
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Open Folder Error", str(exc))

    # ----------------------------------------------------------------------
    # UI queue
    # ----------------------------------------------------------------------

    def _process_ui_queue(self):
        processed = 0
        try:
            while processed < 60:
                try:
                    message_type, data = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if message_type == "TASK_STATUS":
                        task_id, status = data
                        task = self.tasks.get(task_id)
                        if task:
                            task.status = status
                        row = self.row_widgets.get(task_id)
                        if row:
                            try:
                                row.update_status(status)
                            except Exception:
                                pass

                    elif message_type == "TASK_PROGRESS":
                        task_id, progress = data
                        task = self.tasks.get(task_id)
                        if task:
                            task.progress = progress
                        row = self.row_widgets.get(task_id)
                        if row:
                            try:
                                row.update_progress(progress)
                            except Exception:
                                pass

                    elif message_type == "OVERALL_PROGRESS":
                        value = max(0.0, min(1.0, float(data)))
                        self.progress_bar.set(value)

                    elif message_type == "STATUS":
                        self._set_status(str(data))

                    elif message_type == "LOG":
                        self._append_log(str(data))

                    elif message_type == "BULK_ADD_PATHS":
                        try:
                            paths, was_cancelled = data
                        except (TypeError, ValueError):
                            paths, was_cancelled = [], False
                        added = 0
                        for p in paths:
                            if self._add_path(p):
                                added += 1
                        self.scan_in_progress = False
                        if not self.is_converting:
                            self.btn_add.configure(state="normal")
                            self.btn_add_folder.configure(state="normal")
                            self.btn_convert.configure(state="normal")
                        self._refresh_cancel_button()
                        if was_cancelled:
                            self._set_status(
                                f"Scan cancelled · Added {added} file(s) · Total: {len(self.tasks)}"
                            )
                            self._append_log(
                                f"Folder scan cancelled by user · Kept {added} file(s) discovered so far."
                            )
                        else:
                            self._set_status(
                                f"Added {added} file(s) from folder · Total: {len(self.tasks)}"
                            )

                    elif message_type == "SCAN_ERROR":
                        error_msg = str(data)
                        self.scan_in_progress = False
                        if not self.is_converting:
                            self.btn_add.configure(state="normal")
                            self.btn_add_folder.configure(state="normal")
                            self.btn_convert.configure(state="normal")
                        self._refresh_cancel_button()
                        messagebox.showerror(
                            "Folder Error",
                            f"Không thể quét thư mục:\n\n{error_msg}",
                        )

                    elif message_type == "FINISHED":
                        summary = data
                        self._set_ui_state(False)
                        if not self.closing:
                            if summary["cancelled"] > 0:
                                self._set_status(
                                    "Conversion cancelled · "
                                    f"{summary['completed']} completed · "
                                    f"{summary['failed']} failed · "
                                    f"{summary['skipped']} skipped"
                                )
                            elif summary["failed"] > 0:
                                messagebox.showwarning(
                                    "Conversion Finished",
                                    (
                                        f"Completed: {summary['completed']}\n"
                                        f"Failed: {summary['failed']}\n"
                                        f"Skipped: {summary['skipped']}"
                                    ),
                                )
                            else:
                                messagebox.showinfo(
                                    "Conversion Finished",
                                    (
                                        f"Completed: {summary['completed']}\n"
                                        f"Skipped: {summary['skipped']}"
                                    ),
                                )
                except Exception as exc:
                    print(f"[ui-queue] handler error: {exc}", file=sys.stderr)
                finally:
                    self.ui_queue.task_done()
                    processed += 1
        finally:
            if not self.closing:
                self.after(50, self._process_ui_queue)

    # ----------------------------------------------------------------------
    # Conversion
    # ----------------------------------------------------------------------

    def _read_profile(self) -> ConversionProfile:
        fmt = self.combo_format.get().strip().upper()
        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(f"Định dạng không được hỗ trợ: {fmt!r}")
        bitrate = self.combo_bitrate.get().strip()
        parse_bitrate_kbps(bitrate)
        sample_rate = self.combo_samplerate.get().strip()
        if sample_rate not in SAMPLE_RATES_ALL and sample_rate not in SAMPLE_RATES_MP3:
            raise ValueError(f"Sample rate không hợp lệ: {sample_rate!r}")
        if fmt == "MP3" and sample_rate != "Auto":
            try:
                rate = int(sample_rate.split()[0])
            except (ValueError, IndexError):
                raise ValueError(f"Sample rate không hợp lệ: {sample_rate!r}")
            if rate not in {44100, 48000}:
                raise ValueError(
                    "MP3 chỉ hỗ trợ 44100 Hz hoặc 48000 Hz.\n"
                    "Chọn sample rate khác hoặc đổi format."
                )
        conflict_policy = self.combo_conflict.get().strip()
        if conflict_policy not in CONFLICT_POLICIES:
            conflict_policy = DEFAULT_SETTINGS["conflict_policy"]
        return ConversionProfile(
            target_format=fmt,
            bitrate=bitrate,
            sample_rate=sample_rate,
            conflict_policy=conflict_policy,
        )

    def start_conversion(self):
        if self.is_converting:
            return
        if self.scan_in_progress:
            messagebox.showwarning(
                "Scan In Progress",
                "Đang quét thư mục. Hãy đợi quét xong hoặc huỷ quét trước.",
            )
            return
        if not self.tasks:
            messagebox.showwarning("No Files", "Không có file nào trong danh sách.")
            return

        enabled_tasks = [t for t in self.tasks.values() if t.enabled]
        if not enabled_tasks:
            messagebox.showwarning(
                "No Enabled Files",
                "Không có file nào được bật.\n\n"
                "Hãy tick ✓ vào file bạn muốn chuyển đổi.",
            )
            return

        valid_tools, tool_message = AudioEngine.validate_tools()
        if not valid_tools:
            messagebox.showerror("FFmpeg Error", tool_message)
            return

        output_raw = self.entry_output.get().strip()
        if not output_raw:
            messagebox.showwarning("Output Directory", "Hãy chọn thư mục output.")
            return

        try:
            profile = self._read_profile()
        except ValueError as exc:
            messagebox.showerror("Invalid Options", str(exc))
            return

        out_dir = Path(output_raw).expanduser().resolve()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            write_test = out_dir / f".audio_converter_write_test_{uuid.uuid4().hex}"
            write_test.touch()
            write_test.unlink(missing_ok=True)
        except Exception as exc:
            messagebox.showerror("Output Directory Error", f"Không thể ghi vào thư mục output:\n\n{exc}")
            return

        for t in enabled_tasks:
            t.status = "Pending"
            t.progress = 0.0
            t.error_message = ""
            row = self.row_widgets.get(t.task_id)
            if row:
                try:
                    row.update_status("Pending")
                    row.update_progress(0.0)
                except Exception:
                    pass

        task_snapshot = enabled_tasks
        skipped_disabled = len(self.tasks) - len(enabled_tasks)

        self.completed_count = 0
        self.failed_count = 0
        self.skipped_count = 0
        self.cancelled_count = 0

        self.cancel_event.clear()
        self.progress_bar.set(0)
        self.full_logs.clear()
        self._clear_visible_log()

        self._set_ui_state(True)
        self._append_log(f"Starting conversion: {len(task_snapshot)} file(s)")
        if skipped_disabled > 0:
            self._append_log(f"Disabled (skipped): {skipped_disabled} file(s)")
        self._append_log(f"Output: {out_dir}")
        self._append_log(f"Format: {profile.target_format}")
        self._append_log(f"Bitrate: {profile.bitrate}")
        self._append_log(f"Sample rate: {profile.sample_rate}")
        self._append_log(f"Existing policy: {profile.conflict_policy}")

        self.worker_thread = threading.Thread(
            target=self._worker_thread,
            args=(task_snapshot, profile, out_dir),
            name="AudioConverterWorker", daemon=True,
        )
        self.worker_thread.start()

    @staticmethod
    def _same_path(a: Path, b: Path) -> bool:
        try:
            return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
        except Exception:
            return os.path.normcase(str(a)) == os.path.normcase(str(b))

    @staticmethod
    def _unique_rename_path(path: Path) -> Path:
        if not path.exists():
            return path
        counter = 1
        while True:
            candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    def _get_target_path(self, task, profile, out_dir):
        target = out_dir / f"{task.source_path.stem}{profile.extension}"
        if self._same_path(task.source_path, target):
            target = self._unique_rename_path(target)
            return target, "renamed_source_collision"
        if not target.exists():
            return target, "new"
        policy = profile.conflict_policy.lower()
        if policy == "overwrite":
            return target, "overwrite"
        if policy == "skip":
            return target, "skip"
        if policy == "rename":
            return self._unique_rename_path(target), "rename"
        return target, "overwrite"

    def _set_current_process(self, process):
        with self.process_lock:
            self.current_process = process

    def _terminate_current_process(self):
        with self.process_lock:
            process = self.current_process
        if not process:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    process.kill()
        except Exception:
            pass

    def cancel_conversion(self):
        if self.scan_in_progress:
            self.scan_cancel_event.set()
            self.btn_cancel.configure(state="disabled")
            self.ui_queue.put(("STATUS", "Cancelling folder scan..."))
            self.ui_queue.put(("LOG", "Folder scan cancellation requested."))
            return
        if not self.is_converting or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.btn_cancel.configure(state="disabled")
        self.ui_queue.put(("STATUS", "Cancelling conversion..."))
        self.ui_queue.put(("LOG", "Cancellation requested."))
        self._terminate_current_process()

    def _worker_thread(self, tasks, profile, out_dir):
        total = len(tasks)
        completed = 0
        failed = 0
        skipped = 0
        cancelled = 0
        last_index_started = -1
        conversion_start_time = time.monotonic()

        def compute_eta_suffix(finished_count):
            if finished_count <= 0 or total <= 0:
                return ""
            elapsed = time.monotonic() - conversion_start_time
            if elapsed <= 0:
                return ""
            avg = elapsed / finished_count
            remaining = max(0, total - finished_count)
            if remaining == 0:
                return ""
            return f" · ETA {format_duration(avg * remaining)}"

        try:
            for index, task in enumerate(tasks):
                last_index_started = index
                if self.cancel_event.is_set():
                    for remaining in tasks[index:]:
                        cancelled += 1
                        self.ui_queue.put(("TASK_STATUS", (remaining.task_id, "Cancelled")))
                        self.ui_queue.put(("TASK_PROGRESS", (remaining.task_id, 0.0)))
                    break

                finished_before = completed + failed + skipped + cancelled
                eta_suffix = compute_eta_suffix(finished_before)

                self.ui_queue.put(("TASK_STATUS", (task.task_id, "Probing...")))
                self.ui_queue.put(
                    (
                        "STATUS",
                        f"Preparing [{index + 1}/{total}]: {task.source_path.name}{eta_suffix}",
                    )
                )

                temp_path = None
                try:
                    if not task.source_path.exists():
                        raise FileNotFoundError(f"Source file không tồn tại:\n{task.source_path}")

                    source_probe = AudioEngine.probe(task.source_path)
                    task._source_duration = source_probe.duration

                    if self.cancel_event.is_set():
                        cancelled += 1
                        self.ui_queue.put(("TASK_STATUS", (task.task_id, "Cancelled")))
                        self.ui_queue.put(("TASK_PROGRESS", (task.task_id, 0.0)))
                        self.ui_queue.put(("LOG", f"Cancelled: {task.source_path.name}"))
                        continue

                    self.ui_queue.put(
                        (
                            "LOG",
                            f"[{index + 1}/{total}] {task.source_path.name} | "
                            f"codec={source_probe.codec} | rate={source_probe.sample_rate} Hz | "
                            f"channels={source_probe.channels} | duration={source_probe.duration:.2f}s",
                        )
                    )

                    if profile.format_lower == "mp3" and source_probe.channels > 2:
                        raise RuntimeError(
                            "Nguồn có hơn 2 kênh.\n\n"
                            "MP3 không phù hợp để giữ nguyên multichannel. "
                            "Bản này không tự ý downmix để tránh làm thay đổi nguồn."
                        )

                    target_path, target_mode = self._get_target_path(task, profile, out_dir)

                    if target_mode == "skip":
                        skipped += 1
                        self.ui_queue.put(("TASK_STATUS", (task.task_id, "Skipped")))
                        self.ui_queue.put(("TASK_PROGRESS", (task.task_id, 1.0)))
                        self.ui_queue.put(("OVERALL_PROGRESS", (index + 1) / total))
                        self.ui_queue.put(("LOG", f"Skipped existing output: {target_path.name}"))
                        continue

                    temp_path = out_dir / f".{target_path.stem}.{uuid.uuid4().hex}.part{target_path.suffix}"

                    task.progress = 0.0
                    self.ui_queue.put(("TASK_PROGRESS", (task.task_id, 0.0)))
                    self.ui_queue.put(("TASK_STATUS", (task.task_id, "Converting...")))
                    self.ui_queue.put(
                        (
                            "STATUS",
                            f"Converting [{index + 1}/{total}]: {task.source_path.name}{eta_suffix}",
                        )
                    )

                    success = False
                    last_error = ""
                    interrupted = False

                    for attempt in range(2):
                        if self.cancel_event.is_set():
                            cancelled += 1
                            interrupted = True
                            self.ui_queue.put(("TASK_STATUS", (task.task_id, "Cancelled")))
                            self.ui_queue.put(("LOG", f"Cancelled: {task.source_path.name}"))
                            break

                        include_artwork = (attempt == 0) and (source_probe.cover_stream_index is not None)

                        try:
                            if temp_path.exists():
                                temp_path.unlink(missing_ok=True)

                            cmd = AudioEngine.build_command(
                                task=task, profile=profile,
                                temp_output_path=temp_path,
                                probe=source_probe,
                                include_artwork=include_artwork,
                            )

                            self.ui_queue.put(
                                (
                                    "LOG",
                                    f"FFmpeg attempt {attempt + 1}: "
                                    + " ".join(f'"{x}"' if " " in x else x for x in cmd),
                                )
                            )

                            returncode, stderr_lines, was_cancelled = AudioEngine.run_ffmpeg(
                                task=task, cmd=cmd,
                                cancel_event=self.cancel_event,
                                set_process=self._set_current_process,
                                progress_callback=lambda value, tid=task.task_id, idx=index, tot=total:
                                    self._queue_progress(tid, value, idx, tot),
                            )

                            if was_cancelled or self.cancel_event.is_set():
                                raise InterruptedError("Conversion cancelled.")

                            if returncode != 0:
                                last_error = "\n".join(stderr_lines[-20:]) or f"FFmpeg exit code: {returncode}"
                                if attempt == 0 and include_artwork:
                                    self.ui_queue.put(("LOG", "First encode failed. Retrying without artwork..."))
                                    continue
                                raise RuntimeError(last_error)

                            self.ui_queue.put(("TASK_STATUS", (task.task_id, "Verifying...")))
                            verified, verify_message = AudioEngine.verify_output(
                                temp_path, profile, source_probe
                            )

                            if self.cancel_event.is_set():
                                raise InterruptedError("Conversion cancelled.")

                            if not verified:
                                last_error = verify_message
                                if attempt == 0 and include_artwork:
                                    self.ui_queue.put(
                                        ("LOG", "Verification failed with artwork. Retrying without artwork...")
                                    )
                                    continue
                                raise RuntimeError(verify_message)

                            temp_path.replace(target_path)
                            success = True
                            completed += 1

                            self.ui_queue.put(("TASK_PROGRESS", (task.task_id, 1.0)))
                            self.ui_queue.put(("TASK_STATUS", (task.task_id, "Completed")))
                            self.ui_queue.put(("LOG", f"Completed: {target_path.name} | verified OK"))
                            break

                        except InterruptedError:
                            cancelled += 1
                            interrupted = True
                            self.ui_queue.put(("TASK_STATUS", (task.task_id, "Cancelled")))
                            self.ui_queue.put(("LOG", f"Cancelled: {task.source_path.name}"))
                            break

                        except Exception as exc:
                            last_error = str(exc)
                            if (
                                attempt == 0
                                and source_probe.cover_stream_index is not None
                                and profile.format_lower in {"mp3", "flac", "m4a", "alac"}
                            ):
                                self.ui_queue.put(
                                    ("LOG", "Encode/verify failed with artwork. Retrying without artwork...")
                                )
                                continue
                            break

                    if self.cancel_event.is_set():
                        if not success and not interrupted:
                            cancelled += 1
                            self.ui_queue.put(("TASK_STATUS", (task.task_id, "Cancelled")))
                        continue

                    if not success:
                        failed += 1
                        task.error_message = last_error or "Unknown FFmpeg error."
                        self.ui_queue.put(("TASK_STATUS", (task.task_id, "Failed")))
                        self.ui_queue.put(("LOG", f"FAILED: {task.source_path.name}\n{task.error_message}"))

                    self.ui_queue.put(("OVERALL_PROGRESS", (index + 1) / total))

                except Exception as exc:
                    failed += 1
                    task.error_message = str(exc)
                    self.ui_queue.put(("TASK_STATUS", (task.task_id, "Failed")))
                    self.ui_queue.put(("LOG", f"FAILED: {task.source_path.name}\n{exc}"))
                    self.ui_queue.put(("OVERALL_PROGRESS", (index + 1) / total))

                finally:
                    if temp_path and temp_path.exists():
                        for _ in range(3):
                            try:
                                temp_path.unlink(missing_ok=True)
                                break
                            except OSError:
                                time.sleep(0.1)

        except Exception as exc:
            self.ui_queue.put(("LOG", f"WORKER FATAL ERROR:\n{exc}"))
            start_index = last_index_started if last_index_started >= 0 else 0
            for task in tasks[start_index:]:
                if task.status in {"Completed", "Skipped", "Failed", "Cancelled"}:
                    continue
                failed += 1
                task.error_message = f"Worker aborted: {exc}"
                self.ui_queue.put(("TASK_STATUS", (task.task_id, "Failed")))
                self.ui_queue.put(("LOG", f"FAILED (aborted): {task.source_path.name}\n{exc}"))

        finally:
            self._set_current_process(None)
            self.completed_count = completed
            self.failed_count = failed
            self.skipped_count = skipped
            self.cancelled_count = cancelled

            self.ui_queue.put(
                (
                    "STATUS",
                    "Conversion cancelled"
                    if self.cancel_event.is_set()
                    else f"Finished · {completed} completed · {failed} failed · {skipped} skipped",
                )
            )
            self.ui_queue.put(
                (
                    "FINISHED",
                    {
                        "completed": completed,
                        "failed": failed,
                        "skipped": skipped,
                        "cancelled": cancelled,
                    },
                )
            )

    def _queue_progress(self, task_id, task_progress, index, total):
        task_progress = max(0.0, min(1.0, task_progress))
        self.ui_queue.put(("TASK_PROGRESS", (task_id, task_progress)))
        self.ui_queue.put(("OVERALL_PROGRESS", (index + task_progress) / total))

    def on_closing(self):
        if self.closing:
            return
        if self.is_converting or self.scan_in_progress:
            answer = messagebox.askokcancel(
                "Exit",
                "Đang có tác vụ chạy (conversion hoặc quét thư mục).\n\n"
                "Dừng và thoát chương trình?",
            )
            if not answer:
                return
            self.closing = True
            self._save_settings()
            self.cancel_event.set()
            self.scan_cancel_event.set()
            self._terminate_current_process()
            self.withdraw()
            deadline = time.monotonic() + WORKER_CLOSE_TIMEOUT_SECONDS
            self.after(50, lambda: self._wait_for_worker_before_close(deadline))
            return
        self._save_settings()
        self.destroy()

    def _wait_for_worker_before_close(self, deadline):
        worker_alive = self.worker_thread is not None and self.worker_thread.is_alive()
        scan_alive = self.scan_thread is not None and self.scan_thread.is_alive()
        process_alive = False
        with self.process_lock:
            process = self.current_process
        if process:
            try:
                process_alive = process.poll() is None
            except Exception:
                process_alive = False
        if worker_alive or scan_alive or process_alive:
            if time.monotonic() >= deadline:
                self._terminate_current_process()
                try:
                    self.destroy()
                except Exception:
                    pass
                return
            self.after(50, lambda: self._wait_for_worker_before_close(deadline))
            return
        self.destroy()


# ==============================================================================
# ENTRY POINT
# ==============================================================================

def main():
    app = AudioConverterApp()
    app.mainloop()


if __name__ == "__main__":
    main()