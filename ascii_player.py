#!/usr/bin/env python3
"""
ASCII Video Player — CLI utility for playing videos in the terminal
as colorful ASCII/ANSI art.

Features:
  • Local files and YouTube links (via yt-dlp)
  • OpenCV or FFmpeg decoding backends
  • True Color ANSI rendering (half-block + classic ASCII modes)
  • Background decode + render pipeline for low latency
  • Keyboard controls: pause, seek, speed, mute, filters
  • Visual filters: grayscale, sepia, invert, edge, matrix rain
  • Progress bar with timestamps
  • Webcam live view
  • Single-image display
  • SRT subtitle overlay
  • Session recording to file
  • Quality presets (low / med / high / ultra)
"""

# ─── Imports ──────────────────────────────────────────────────────────────────

import argparse
import sys
import time
import os
import re
import subprocess
import shutil
import io
import json
import threading
import collections
import tempfile
import uuid
import random
import atexit

# Optional OpenCV import
try:
    import cv2
except ImportError:
    cv2 = None

import numpy as np

# Platform-specific keyboard input
if sys.platform == "win32":
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
else:
    msvcrt = None

if sys.platform != "win32":
    try:
        import termios
        import tty
        import select as _select
    except ImportError:
        termios = tty = _select = None
else:
    termios = tty = _select = None

# Force UTF-8 for stdout/stderr on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ─── Constants ────────────────────────────────────────────────────────────────

ASCII_CHARS = " .:-=+*#%@"
CHAR_ASPECT_RATIO = 0.55
HALF_BLOCK = "\u2580"  # ▀

HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
MOVE_HOME   = "\033[H"
RESET_COLOR = "\033[0m"
CLEAR_SCREEN = "\033[2J"
ERASE_LINE  = "\033[K"


TEMP_FILES = set()


def restore_cursor() -> None:
    sys.stdout.write(SHOW_CURSOR + RESET_COLOR)
    sys.stdout.flush()
    for path in list(TEMP_FILES):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


atexit.register(restore_cursor)

QUALITY_PRESETS = {
    "low":   {"width": 60,  "quantize": 8},
    "med":   {"width": 100, "quantize": 4},
    "high":  {"width": 140, "quantize": 2},
    "ultra": {"width": 200, "quantize": 0},
}

FILTER_LIST = ["none", "grayscale", "sepia", "invert", "edge", "matrix"]


# ─── Utilities ────────────────────────────────────────────────────────────────

def is_url(path: str) -> bool:
    return bool(re.match(r"https?://", path, re.IGNORECASE))


def is_youtube_url(url: str) -> bool:
    patterns = [
        r"(https?://)?(www\.)?youtube\.com/watch",
        r"(https?://)?(www\.)?youtube\.com/shorts",
        r"(https?://)?youtu\.be/",
        r"(https?://)?(www\.)?youtube\.com/embed",
    ]
    return any(re.match(p, url, re.IGNORECASE) for p in patterns)


def get_stream_url(youtube_url: str) -> str:
    print(f"\033[33m⏳ Fetching stream from YouTube: {youtube_url}\033[0m")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp",
             "--format", "best[height<=720]", "--get-url", youtube_url],
            capture_output=True, text=True, check=True, timeout=30,
        )
        stream_url = result.stdout.strip()
        if not stream_url:
            raise RuntimeError("yt-dlp returned an empty URL.")
        print("\033[32m✅ Stream URL obtained.\033[0m")
        return stream_url
    except FileNotFoundError:
        print("\033[31m❌ yt-dlp not found. Install it: pip install yt-dlp\033[0m", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"\033[31m❌ yt-dlp error:\n{e.stderr}\033[0m", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("\033[31m❌ yt-dlp: timeout exceeded (30s).\033[0m", file=sys.stderr)
        sys.exit(1)


def check_and_add_ffmpeg_to_path() -> None:
    if shutil.which("ffmpeg") and shutil.which("ffplay"):
        return
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            pkgs = os.path.join(local, "Microsoft", "WinGet", "Packages")
            if os.path.isdir(pkgs):
                for d in os.listdir(pkgs):
                    if "ffmpeg" in d.lower() or "ffplay" in d.lower():
                        pkg_dir = os.path.join(pkgs, d)
                        for root, _dirs, files in os.walk(pkg_dir):
                            if "ffmpeg.exe" in files:
                                os.environ["PATH"] = root + os.pathsep + os.environ["PATH"]
                                return


# ─── SRT Subtitle Parser ─────────────────────────────────────────────────────

def _srt_ts_to_sec(ts: str) -> float:
    ts = ts.replace(",", ".")
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_srt(filepath: str) -> list:
    """Return list of (start_sec, end_sec, text)."""
    subs: list = []
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(filepath, "r", encoding="latin-1") as f:
            content = f.read()

    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.strip().split("\n")
        time_line = None
        text_start = 0
        for i, ln in enumerate(lines):
            if "-->" in ln:
                time_line = ln
                text_start = i + 1
                break
        if not time_line:
            continue
        m = re.match(r"(\d+:\d+:\d+[.,]\d+)\s*-->\s*(\d+:\d+:\d+[.,]\d+)", time_line)
        if not m:
            continue
        start = _srt_ts_to_sec(m.group(1))
        end = _srt_ts_to_sec(m.group(2))
        text = " ".join(lines[text_start:])
        text = re.sub(r"<[^>]+>", "", text)
        subs.append((start, end, text))
    return subs


def get_current_subtitle(subs: list, t: float) -> str | None:
    for start, end, text in subs:
        if start <= t <= end:
            return text
    return None


# ─── FFmpeg Video Decoder ────────────────────────────────────────────────────

class FFmpegVideoCapture:
    """Decodes video via an FFmpeg subprocess pipe, scaling during decode."""

    def __init__(self, source: str, width: int, height: int, fps: float = 24.0):
        self.source = source
        self.width = width
        self.height = height
        self.fps = fps
        self.process = None
        self.frame_size = width * height * 3

    def isOpened(self) -> bool:
        return shutil.which("ffmpeg") is not None

    def start(self, ss: float = 0) -> bool:
        cmd = ["ffmpeg", "-y", "-v", "error"]
        if ss > 0:
            cmd += ["-ss", f"{ss:.2f}"]
        cmd += [
            "-i", self.source,
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-vf", f"scale={self.width}:{self.height}",
            "-",
        ]
        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=self.frame_size * 4,
            )
            return True
        except Exception:
            self.process = None
            return False

    def read(self):
        if self.process is None:
            if not self.start():
                return False, None
        try:
            data = self.process.stdout.read(self.frame_size)
            if len(data) < self.frame_size:
                return False, None
            frame = np.frombuffer(data, dtype=np.uint8).reshape(
                (self.height, self.width, 3)
            )
            return True, frame
        except Exception:
            return False, None

    # duck-typing for cv2.VideoCapture compatibility
    def set(self, prop, val):
        # 1 corresponds to cv2.CAP_PROP_POS_FRAMES
        if prop == 1 or (cv2 is not None and prop == cv2.CAP_PROP_POS_FRAMES):
            self.release()
            ss = val / self.fps if self.fps > 0 else 0
            return self.start(ss=ss)
        return False

    def get(self, prop):
        """Minimal duck-typed get() for properties needed by play_video."""
        if cv2 is not None:
            if prop == cv2.CAP_PROP_FPS:
                return self.fps
            if prop == cv2.CAP_PROP_FRAME_COUNT:
                return 0  # unknown for pipe decoder
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                return self.width
            if prop == cv2.CAP_PROP_FRAME_HEIGHT:
                return self.height
        # numeric fallbacks: 5=FPS, 7=FRAME_COUNT, 3=WIDTH, 4=HEIGHT
        _map = {5: self.fps, 7: 0, 3: self.width, 4: self.height}
        return _map.get(prop, 0)

    def release(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=1.0)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None


def get_video_properties_ffprobe(source: str) -> tuple[float, int, int, int]:
    """Return (fps, total_frames, width, height) via ffprobe."""

    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,nb_frames,width,height",
        "-of", "json", source,
    ]
    fps, total, w, h = 24.0, 0, 120, 60
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(r.stdout)
        streams = data.get("streams", [])
        if streams:
            s = streams[0]
            fps_str = s.get("avg_frame_rate", "")
            if fps_str and fps_str != "N/A":
                if "/" in fps_str:
                    try:
                        n, d = map(float, fps_str.split("/"))
                        if d:
                            fps = n / d
                    except ValueError:
                        pass
                else:
                    try:
                        fps = float(fps_str)
                    except ValueError:
                        pass
            nb = s.get("nb_frames", "")
            if nb and nb != "N/A":
                try:
                    total = int(nb)
                except ValueError:
                    pass
            for key, target in [("width", "w"), ("height", "h")]:
                v = s.get(key)
                if v is not None:
                    try:
                        if target == "w":
                            w = int(v)
                        else:
                            h = int(v)
                    except ValueError:
                        pass
    except Exception:
        pass
    return fps, total, w, h


# ─── Audio Playback ──────────────────────────────────────────────────────────

def _ffplay_cmd(source: str, volume: int, ss: float = 0) -> list:
    cmd = [
        "ffplay", "-nodisp", "-autoexit", "-volume", str(volume),
        "-fflags", "nobuffer", "-flags", "low_delay",
    ]
    if is_url(source):
        cmd += ["-probesize", "32", "-analyzeduration", "0"]
    if ss > 0:
        cmd += ["-ss", f"{ss:.1f}"]
    cmd.append(source)
    return cmd


def extract_audio(source: str) -> str | None:
    tmp = os.path.join(tempfile.gettempdir(), f"ascii_audio_{uuid.uuid4().hex}.wav")
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", source,
        "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", tmp,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        TEMP_FILES.add(tmp)
        return tmp
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        return None


class AudioController:
    """Controls background audio playback with seek / mute / volume."""

    def __init__(self, source: str, volume: int = 100, use_ffplay: bool = False):
        self.source = source
        self.use_ffplay = use_ffplay or is_url(source)
        self.volume = volume
        self.temp_wav: str | None = None
        self.ffplay_proc: subprocess.Popen | None = None
        self.muted = False
        self.suspended = False
        self._active = False

    # ── public API ────────────────────────────────────────────────────────

    def start(self, position: float = 0.0) -> bool:
        if self.muted:
            return False
        self._stop_playback()

        # Prefer ffplay (streams directly, supports seeking)
        if shutil.which("ffplay"):
            try:
                self.ffplay_proc = subprocess.Popen(
                    _ffplay_cmd(self.source, self.volume, position),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self._active = True
                self.suspended = False
                return True
            except Exception:
                pass

        # Windows winsound fallback (only for local files, no seeking)
        if sys.platform == "win32" and shutil.which("ffmpeg") and position == 0:
            self.temp_wav = extract_audio(self.source)
            if self.temp_wav:
                try:
                    import winsound
                    winsound.PlaySound(
                        self.temp_wav, winsound.SND_FILENAME | winsound.SND_ASYNC
                    )
                    self._active = True
                    return True
                except Exception:
                    pass
        return False

    def seek(self, position: float) -> None:
        if self.muted:
            return
        self.start(position=position)

    def pause(self) -> None:
        if self.ffplay_proc and not self.suspended:
            if sys.platform == "win32":
                try:
                    import ctypes
                    h = ctypes.windll.kernel32.OpenProcess(0x0800, False, self.ffplay_proc.pid)
                    if h and h != 0:
                        ctypes.windll.ntdll.NtSuspendProcess(h)
                        ctypes.windll.kernel32.CloseHandle(h)
                        self.suspended = True
                except Exception:
                    pass
            else:
                try:
                    import signal
                    os.kill(self.ffplay_proc.pid, signal.SIGSTOP)
                    self.suspended = True
                except Exception:
                    pass

    def resume(self) -> None:
        if self.ffplay_proc and self.suspended:
            if sys.platform == "win32":
                try:
                    import ctypes
                    h = ctypes.windll.kernel32.OpenProcess(0x0800, False, self.ffplay_proc.pid)
                    if h and h != 0:
                        ctypes.windll.ntdll.NtResumeProcess(h)
                        ctypes.windll.kernel32.CloseHandle(h)
                        self.suspended = False
                except Exception:
                    pass
            else:
                try:
                    import signal
                    os.kill(self.ffplay_proc.pid, signal.SIGCONT)
                    self.suspended = False
                except Exception:
                    pass

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        if self.muted:
            self._stop_playback()
            self._active = False
        return self.muted

    def volume_down(self) -> int:
        self.volume = max(0, self.volume - 10)
        if self.ffplay_proc and self.ffplay_proc.stdin:
            try:
                self.ffplay_proc.stdin.write(b"9")
                self.ffplay_proc.stdin.flush()
            except Exception:
                pass
        return self.volume

    def volume_up(self) -> int:
        self.volume = min(100, self.volume + 10)
        if self.ffplay_proc and self.ffplay_proc.stdin:
            try:
                self.ffplay_proc.stdin.write(b"0")
                self.ffplay_proc.stdin.flush()
            except Exception:
                pass
        return self.volume

    def stop(self) -> None:
        self._stop_playback()
        self._active = False
        if self.temp_wav:
            if sys.platform == "win32":
                try:
                    import winsound
                    winsound.PlaySound(None, winsound.SND_PURGE)
                except Exception:
                    pass
                time.sleep(0.1)
            for _ in range(5):
                try:
                    if os.path.exists(self.temp_wav):
                        os.remove(self.temp_wav)
                    break
                except PermissionError:
                    time.sleep(0.1)
                except Exception:
                    break
            self.temp_wav = None

    # ── internals ─────────────────────────────────────────────────────────

    def _stop_playback(self) -> None:
        if self.ffplay_proc:
            if self.suspended and sys.platform != "win32":
                try:
                    self.resume()
                except Exception:
                    pass
            try:
                if self.ffplay_proc.stdin:
                    self.ffplay_proc.stdin.close()
                self.ffplay_proc.terminate()
                self.ffplay_proc.wait(timeout=1.0)
            except Exception:
                try:
                    self.ffplay_proc.kill()
                except Exception:
                    pass
            self.ffplay_proc = None
            self.suspended = False

        if self.temp_wav and sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass


# ─── Frame Buffer (background decoder thread) ────────────────────────────────

class FrameBuffer:
    """
    Background thread that continuously reads raw frames from the video
    source and stores them in a ring buffer.
    """

    def __init__(self, cap, buffer_size: int = 120):
        self.cap = cap
        self.buffer: collections.deque = collections.deque(maxlen=buffer_size)
        self.lock = threading.Lock()
        self.cap_lock = threading.Lock()
        self.frame_index = 0
        self.finished = False
        self._paused = threading.Event()
        self._paused.set()          # running by default
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reader, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _reader(self) -> None:
        while not self._stop.is_set():
            self._paused.wait()     # block while paused
            if self._stop.is_set():
                break

            # back-pressure: wait if buffer full
            while not self._stop.is_set():
                with self.lock:
                    if self.buffer.maxlen is None or len(self.buffer) < self.buffer.maxlen:
                        break
                time.sleep(0.003)

            if self._stop.is_set():
                break

            with self.cap_lock:
                ret, frame = self.cap.read()
            if not ret:
                self.finished = True
                break
            with self.lock:
                self.buffer.append((self.frame_index, frame))
                self.frame_index += 1

    # ── API ───────────────────────────────────────────────────────────────

    def prebuffer(self, count: int = 10, timeout: float = 5.0) -> None:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            with self.lock:
                if len(self.buffer) >= count or self.finished:
                    return
            time.sleep(0.01)

    def get_next(self):
        """Pop the oldest buffered frame (for sequential consumption)."""
        with self.lock:
            if self.buffer:
                return self.buffer.popleft()
            return None, None

    def seek(self, frame_index: int) -> bool:
        """Seek to *frame_index*. Returns True if the backend supports it."""
        self._paused.clear()

        with self.cap_lock:
            with self.lock:
                self.buffer.clear()

            seekable = False
            if isinstance(self.cap, FFmpegVideoCapture):
                # FFmpegVideoCapture.set() restarts the pipe with -ss
                self.cap.set(1, frame_index)  # 1 == CAP_PROP_POS_FRAMES
                seekable = True
            elif cv2 is not None and isinstance(self.cap, cv2.VideoCapture):
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                seekable = True

            if seekable:
                self.frame_index = frame_index
                self.finished = False

        self._paused.set()
        return seekable

    def stop_thread(self) -> None:
        self._stop.set()
        self._paused.set()          # unblock if waiting
        if self._thread.is_alive():
            try:
                self._thread.join(timeout=1.0)
            except Exception:
                pass

    @property
    def is_finished(self) -> bool:
        with self.lock:
            return self.finished and len(self.buffer) == 0


# ─── Visual Filters ──────────────────────────────────────────────────────────

def _filter_grayscale(frame: np.ndarray) -> np.ndarray:
    gray = np.dot(frame[..., :3].astype(np.float32), [0.114, 0.587, 0.299])
    gray = gray.astype(np.uint8)
    return np.stack([gray, gray, gray], axis=-1)


def _filter_sepia(frame: np.ndarray) -> np.ndarray:
    f = frame.astype(np.float32)
    # BGR order
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    out_r = np.clip(r * 0.393 + g * 0.769 + b * 0.189, 0, 255)
    out_g = np.clip(r * 0.349 + g * 0.686 + b * 0.168, 0, 255)
    out_b = np.clip(r * 0.272 + g * 0.534 + b * 0.131, 0, 255)
    return np.stack([out_b, out_g, out_r], axis=-1).astype(np.uint8)


def _filter_invert(frame: np.ndarray) -> np.ndarray:
    return (255 - frame).astype(np.uint8)


def _filter_edge(frame: np.ndarray) -> np.ndarray:
    if cv2 is not None:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 80, 180)
        return cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)
    # Numpy fallback: simple gradient magnitude
    gray = np.dot(frame[..., :3].astype(np.float32), [0.114, 0.587, 0.299])
    dx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    dy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    mag = np.clip(np.sqrt(dx ** 2 + dy ** 2) * 3, 0, 255).astype(np.uint8)
    return np.stack([mag, mag, mag], axis=-1)


class MatrixRain:
    """Simulates 'digital rain' columns that overlay onto the frame."""

    def __init__(self, width: int, height: int):
        self.w = width
        self.h = height
        self.drops = np.random.randint(-height, height, size=width).astype(np.float64)
        self.speeds = np.random.uniform(0.5, 3.0, size=width)

    def update(self) -> None:
        self.drops += self.speeds
        reset = self.drops > self.h + 15
        n = reset.sum()
        if n:
            self.drops[reset] = np.random.randint(-self.h, 0, size=n)
            self.speeds[reset] = np.random.uniform(0.5, 3.0, size=n)

    def apply(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        result = frame.astype(np.float32)
        cols = min(w, self.w)
        trail = 14

        col_idx = np.arange(cols)
        offsets = np.arange(trail)
        # y positions: (cols, trail)
        y_pos = (self.drops[:cols, None].astype(int) - offsets[None, :])
        x_pos = np.broadcast_to(col_idx[:, None], (cols, trail))
        alphas = np.broadcast_to(1.0 - offsets / trail, (cols, trail))

        valid = (y_pos >= 0) & (y_pos < h) & (x_pos < w)
        vy = y_pos[valid]
        vx = x_pos[valid]
        va = alphas[valid]

        result[vy, vx, 0] *= (1 - va * 0.65)
        result[vy, vx, 1] = np.clip(
            result[vy, vx, 1] * (1 - va * 0.3) + va * 185, 0, 255
        )
        result[vy, vx, 2] *= (1 - va * 0.65)

        self.update()
        return np.clip(result, 0, 255).astype(np.uint8)


def apply_filter(
    frame: np.ndarray,
    name: str,
    matrix_rain: MatrixRain | None = None,
) -> np.ndarray:
    if name == "grayscale":
        return _filter_grayscale(frame)
    if name == "sepia":
        return _filter_sepia(frame)
    if name == "invert":
        return _filter_invert(frame)
    if name == "edge":
        return _filter_edge(frame)
    if name == "matrix" and matrix_rain is not None:
        return matrix_rain.apply(frame)
    return frame


# ─── Frame → ANSI Rendering ──────────────────────────────────────────────────

def _numpy_resize(frame: np.ndarray, new_w: int, new_h: int) -> np.ndarray:
    """Simple nearest-neighbour resize using numpy (fallback when cv2 is None)."""
    h, w = frame.shape[:2]
    row_idx = (np.arange(new_h) * h / new_h).astype(int)
    col_idx = (np.arange(new_w) * w / new_w).astype(int)
    row_idx = np.clip(row_idx, 0, h - 1)
    col_idx = np.clip(col_idx, 0, w - 1)
    return frame[np.ix_(row_idx, col_idx)]


def _resize_frame(frame: np.ndarray, new_w: int, new_h: int) -> np.ndarray:
    """Resize using cv2 if available, else numpy fallback."""
    if cv2 is not None:
        return cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
    return _numpy_resize(frame, new_w, new_h)


def frame_to_ansi_fast(
    frame: np.ndarray, width: int, already_scaled: bool = False,
    quantize: int = 0,
) -> str:
    """Classic ASCII characters coloured with True Color ANSI."""
    if already_scaled:
        resized = frame
        new_h, new_w = frame.shape[:2]
    else:
        h, w = frame.shape[:2]
        new_h = int(width * (h / w) * CHAR_ASPECT_RATIO)
        new_w = width
        resized = _resize_frame(frame, new_w, new_h)

    rgb = resized[..., ::-1]
    if quantize > 0:
        rgb = (rgb >> quantize) << quantize

    gray = np.dot(rgb[..., :3].astype(np.float32), [0.299, 0.587, 0.114])
    n = len(ASCII_CHARS)
    idx = np.clip((gray / 255.0 * (n - 1)).astype(int), 0, n - 1)
    chars = np.array(list(ASCII_CHARS))
    asc = chars[idx]

    buf = [MOVE_HOME]
    for y in range(new_h):
        row = rgb[y]
        rc = asc[y]
        prev = None
        parts: list[str] = []
        for x in range(new_w):
            r, g, b = int(row[x, 0]), int(row[x, 1]), int(row[x, 2])
            c = (r, g, b)
            ch = rc[x]
            if c != prev:
                parts.append(f"\033[38;2;{r};{g};{b}m{ch}")
                prev = c
            else:
                parts.append(ch)
        buf.append("".join(parts) + ERASE_LINE)
    return RESET_COLOR + "\n".join(buf) + RESET_COLOR


def frame_to_halfblock(
    frame: np.ndarray, width: int, already_scaled: bool = False,
    quantize: int = 0,
) -> str:
    """High-quality half-block rendering (2 vertical pixels per cell)."""
    if already_scaled:
        resized = frame
        ph = frame.shape[0]
        if ph % 2:
            ph -= 1
            resized = resized[:ph]
    else:
        h, w = frame.shape[:2]
        ph = int(width * (h / w))
        if ph % 2:
            ph += 1
        resized = _resize_frame(frame, width, ph)

    ch = ph // 2
    rgb = resized[..., ::-1]
    if quantize > 0:
        rgb = (rgb >> quantize) << quantize

    buf = [MOVE_HOME]
    for cy in range(ch):
        top = rgb[cy * 2]
        bot = rgb[cy * 2 + 1]
        pfg = pbg = None
        parts: list[str] = []
        for x in range(rgb.shape[1]):
            fr, fg_, fb = int(top[x, 0]), int(top[x, 1]), int(top[x, 2])
            br, bg_, bb = int(bot[x, 0]), int(bot[x, 1]), int(bot[x, 2])
            cfg = (fr, fg_, fb)
            cbg = (br, bg_, bb)
            seq = ""
            if cfg != pfg:
                seq += f"\033[38;2;{fr};{fg_};{fb}m"
                pfg = cfg
            if cbg != pbg:
                seq += f"\033[48;2;{br};{bg_};{bb}m"
                pbg = cbg
            parts.append(f"{seq}{HALF_BLOCK}")
        buf.append("".join(parts) + ERASE_LINE)
    return RESET_COLOR + "\n".join(buf) + RESET_COLOR


# ─── Render Pipeline (background thread) ─────────────────────────────────────

class RenderPipeline:
    """
    Takes raw frames from a FrameBuffer, applies the current filter,
    renders them to ANSI strings, and stores the result for the main
    thread to write to stdout.
    """

    def __init__(
        self,
        fb: FrameBuffer,
        render_fn,
        width: int,
        already_scaled: bool = False,
        quantize: int = 0,
        buf_size: int = 30,
    ):
        self.fb = fb
        self.render_fn = render_fn
        self.width = width
        self.already_scaled = already_scaled
        self.quantize = quantize
        self.buffer: collections.deque = collections.deque(maxlen=buf_size)
        self.lock = threading.Lock()
        self.current_filter = "none"
        self._matrix_rain: MatrixRain | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            try:
                self._thread.join(timeout=1.0)
            except Exception:
                pass

    def clear(self) -> None:
        with self.lock:
            self.buffer.clear()

    # ── worker ────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while not self._stop.is_set():
            idx, frame = self.fb.get_next()
            if frame is None:
                if self.fb.is_finished:
                    break
                time.sleep(0.002)
                continue

            filt = self.current_filter

            # Matrix rain lifecycle
            if filt == "matrix":
                if self._matrix_rain is None:
                    h, w = frame.shape[:2]
                    self._matrix_rain = MatrixRain(w, h)
            else:
                self._matrix_rain = None

            frame = apply_filter(frame, filt, self._matrix_rain)

            ansi = self.render_fn(
                frame, self.width,
                already_scaled=self.already_scaled,
                quantize=self.quantize,
            )

            # back-pressure
            while not self._stop.is_set():
                with self.lock:
                    if self.buffer.maxlen is None or len(self.buffer) < self.buffer.maxlen:
                        self.buffer.append((idx, ansi))
                        break
                time.sleep(0.002)

    # ── consumer API ──────────────────────────────────────────────────────

    def get_frame(self, target_idx: int):
        """Skip old frames, return the best available (idx, ansi_str)."""
        with self.lock:
            while self.buffer and self.buffer[0][0] < target_idx - 2:
                self.buffer.popleft()
            if self.buffer and self.buffer[0][0] <= target_idx:
                return self.buffer.popleft()
        return None, None

    @property
    def is_finished(self) -> bool:
        with self.lock:
            return self.fb.is_finished and len(self.fb.buffer) == 0 and len(self.buffer) == 0


# ─── Keyboard Controller ─────────────────────────────────────────────────────

class KeyboardController:
    """Non-blocking keyboard reader (Windows + Unix)."""

    KEYS = {
        "space", "quit", "mute", "filter", "speed_down", "speed_up",
        "left", "right", "volume_up", "volume_down", "info",
    }

    def __init__(self):
        self.queue: collections.deque = collections.deque(maxlen=32)
        self._running = False
        self._thread: threading.Thread | None = None
        self._old_settings = None

    def start(self) -> None:
        if not sys.stdin.isatty():
            return
        if sys.platform == "win32" and msvcrt:
            self._running = True
        elif termios and tty and _select:
            try:
                self._old_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
                self._running = True
            except Exception:
                return
        else:
            return
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._old_settings is not None and termios:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            try:
                self._thread.join(timeout=1.0)
            except Exception:
                pass

    def get_key(self) -> str | None:
        if self.queue:
            return self.queue.popleft()
        return None

    # ── internal readers ──────────────────────────────────────────────────

    def _reader(self) -> None:
        if sys.platform == "win32":
            self._read_win()
        else:
            self._read_unix()

    def _read_win(self) -> None:
        while self._running:
            if msvcrt and msvcrt.kbhit():
                k = msvcrt.getch()
                if k in (b"\xe0", b"\x00"):
                    sc = msvcrt.getch()
                    _map = {b"K": "left", b"M": "right", b"H": "volume_up", b"P": "volume_down"}
                    mapped = _map.get(sc)
                    if mapped:
                        self.queue.append(mapped)
                else:
                    self._map_char(k.decode("utf-8", errors="ignore"))
            time.sleep(0.015)

    def _read_unix(self) -> None:
        while self._running:
            try:
                if _select and _select.select([sys.stdin], [], [], 0.015)[0]:
                    ch = sys.stdin.read(1)
                    if ch == "\x1b":
                        c2 = sys.stdin.read(1) if _select.select([sys.stdin], [], [], 0.05)[0] else ""
                        c3 = sys.stdin.read(1) if c2 and _select.select([sys.stdin], [], [], 0.05)[0] else ""
                        if c2 == "[":
                            _map = {"A": "volume_up", "B": "volume_down", "C": "right", "D": "left"}
                            mapped = _map.get(c3)
                            if mapped:
                                self.queue.append(mapped)
                    else:
                        self._map_char(ch)
            except Exception:
                time.sleep(0.05)

    def _map_char(self, ch: str) -> None:
        ch_low = ch.lower()
        _table = {
            " ": "space", "q": "quit", "m": "mute", "f": "filter",
            "[": "speed_down", "]": "speed_up", "i": "info",
            "-": "volume_down", "=": "volume_up", "+": "volume_up",
        }
        mapped = _table.get(ch_low)
        if mapped:
            self.queue.append(mapped)


# ─── UI Helpers ───────────────────────────────────────────────────────────────

def build_progress_bar(
    video_time: float,
    total_time: float,
    bar_width: int,
    paused: bool,
    speed: float,
    muted: bool,
    volume: int,
    mode: str,
    filter_name: str,
    decoder: str,
) -> str:
    bw = max(bar_width, 10)
    progress = min(video_time / total_time, 1.0) if total_time > 0 else 0
    filled = int(bw * progress)
    bar = "\u2593" * filled + "\u2591" * (bw - filled)

    cm, cs = divmod(int(video_time), 60)
    tm, ts = divmod(int(total_time), 60)

    icon = "\u23f8" if paused else "\u25b6"
    vol = "\U0001f507 Muted" if muted else f"\U0001f50a {volume}%"

    return (
        f"\033[36m{icon} {bar} {cm:02d}:{cs:02d}/{tm:02d}:{ts:02d}"
        f" | {speed:.2f}x | {vol}"
        f" | {mode.upper()} | {filter_name}"
        f" | {decoder.upper()}\033[0m{ERASE_LINE}"
    )


def build_webcam_status(
    fps_actual: float, mode: str, filter_name: str, width: int,
) -> str:
    return (
        f"\033[36m\U0001f4f7 WEBCAM | FPS: {fps_actual:.1f}"
        f" | {mode.upper()} | Filter: {filter_name}"
        f" | W:{width}\033[0m{ERASE_LINE}"
    )


CONTROLS_LINE = (
    "\033[90m  Space:\u23ef  \u2190\u2192:Seek  \u2191\u2193/[-+]:Vol"
    "  []:Speed  M:Mute  F:Filter  Q:Quit  I:Info\033[0m"
)


# ─── Recorder ─────────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self, filepath: str):
        self.file = open(filepath, "w", encoding="utf-8")
        self.count = 0

    def write_frame(self, ansi: str) -> None:
        self.file.write(ansi + "\n")
        self.file.flush()
        self.count += 1

    def close(self) -> None:
        self.file.write(SHOW_CURSOR + RESET_COLOR)
        self.file.close()


# ─── Image Display ────────────────────────────────────────────────────────────

def show_image(
    source: str,
    width: int,
    mode: str = "block",
    filter_name: str = "none",
    quantize: int = 0,
) -> None:
    if cv2 is None:
        print("\033[31m❌ Image mode requires OpenCV (pip install opencv-python).\033[0m",
              file=sys.stderr)
        sys.exit(1)

    img = cv2.imread(source)
    if img is None:
        print(f"\033[31m❌ Failed to load image: {source}\033[0m", file=sys.stderr)
        sys.exit(1)

    # Clamp to terminal
    term_c, term_r = shutil.get_terminal_size((120, 40))
    max_rows = max(term_r - 4, 10)
    h, w = img.shape[:2]
    aspect = h / w
    if mode == "block":
        ph = int(width * aspect)
        if ph % 2:
            ph += 1
        if ph // 2 > max_rows:
            ph = max_rows * 2
            width = int(ph / aspect)
    else:
        th = int(width * aspect * CHAR_ASPECT_RATIO)
        if th > max_rows:
            th = max_rows
            width = int(th / (aspect * CHAR_ASPECT_RATIO))

    # Apply filter
    rain = MatrixRain(width, int(width * aspect)) if filter_name == "matrix" else None
    img = apply_filter(img, filter_name, rain)

    render_fn = frame_to_halfblock if mode == "block" else frame_to_ansi_fast
    ansi = render_fn(img, width, quantize=quantize)

    sys.stdout.write(HIDE_CURSOR + CLEAR_SCREEN + ansi + RESET_COLOR)
    sys.stdout.write(f"\n\033[33m  🖼  Image: {os.path.basename(source)}  |  Press any key to exit\033[0m{ERASE_LINE}")
    sys.stdout.flush()

    # Wait for keypress
    if sys.platform == "win32" and msvcrt:
        msvcrt.getch()
    elif termios and tty:
        old = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
    else:
        input()

    sys.stdout.write(SHOW_CURSOR + RESET_COLOR + CLEAR_SCREEN)
    sys.stdout.flush()


# ─── Main Playback Loop ──────────────────────────────────────────────────────

def play_video(
    source,
    width: int,
    mode: str = "block",
    play_audio: bool = True,
    use_ffplay: bool = False,
    decoder: str = "opencv",
    filter_name: str = "none",
    quantize: int = 0,
    subs_file: str | None = None,
    record_file: str | None = None,
    is_webcam: bool = False,
) -> None:
    check_and_add_ffmpeg_to_path()

    # Decoder fallback
    if decoder == "ffmpeg" and not shutil.which("ffmpeg"):
        decoder = "opencv"
    if decoder == "opencv" and cv2 is None:
        if shutil.which("ffmpeg"):
            decoder = "ffmpeg"
        else:
            print("\033[31m❌ Neither OpenCV nor FFmpeg available.\033[0m", file=sys.stderr)
            sys.exit(1)

    # ── Video properties ──────────────────────────────────────────────────

    fps = 30.0 if is_webcam else 24.0
    total_frames = 0
    orig_w, orig_h = 120, 60
    got_props = False

    if not is_webcam:
        if decoder == "opencv":
            tmp_cap = cv2.VideoCapture(source)
            if tmp_cap.isOpened():
                fps = tmp_cap.get(cv2.CAP_PROP_FPS)
                total_frames = int(tmp_cap.get(cv2.CAP_PROP_FRAME_COUNT))
                orig_w = int(tmp_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                orig_h = int(tmp_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                tmp_cap.release()
                got_props = True
        if not got_props and shutil.which("ffprobe"):
            fps, total_frames, orig_w, orig_h = get_video_properties_ffprobe(source)
            got_props = True
    else:
        # Webcam: open once to detect resolution
        tmp_cap = cv2.VideoCapture(source)
        if tmp_cap.isOpened():
            orig_w = int(tmp_cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
            orig_h = int(tmp_cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
            f = tmp_cap.get(cv2.CAP_PROP_FPS)
            if f > 0:
                fps = f
            tmp_cap.release()

    if fps <= 0:
        fps = 24.0
    frame_time = 1.0 / fps
    total_time = total_frames / fps if total_frames > 0 else 0
    aspect = orig_h / orig_w if orig_w > 0 else 0.5

    # ── Clamp dimensions to terminal ──────────────────────────────────────

    term_cols, term_rows = shutil.get_terminal_size((120, 40))
    max_rows = max(term_rows - 4, 10)  # leave room for status + subtitle

    if mode == "block":
        ph = int(width * aspect)
        if ph % 2:
            ph += 1
        ch = ph // 2
        if ch > max_rows:
            ch = max_rows
            ph = ch * 2
            width = int(ph / aspect) if aspect > 0 else width
        target_h = ph
        target_w = width
    else:
        target_h = int(width * aspect * CHAR_ASPECT_RATIO)
        if target_h > max_rows:
            target_h = max_rows
            width = int(target_h / (aspect * CHAR_ASPECT_RATIO)) if aspect > 0 else width
        target_w = width

    frame_lines = ch if mode == "block" else target_h

    # ── Open capture ──────────────────────────────────────────────────────

    if decoder == "ffmpeg" and not is_webcam:
        cap = FFmpegVideoCapture(source, target_w, target_h, fps=fps)
        if not cap.isOpened():
            print("\033[31m❌ FFmpeg not available.\033[0m", file=sys.stderr)
            sys.exit(1)
    else:
        cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            label = "webcam" if is_webcam else source
            print(f"\033[31m❌ Cannot open: {label}\033[0m", file=sys.stderr)
            sys.exit(1)

    render_fn = frame_to_halfblock if mode == "block" else frame_to_ansi_fast
    already_scaled = (decoder == "ffmpeg" and not is_webcam)

    # ── Splash frame ──────────────────────────────────────────────────────

    ret, first_frame = cap.read()
    if not ret:
        print("\033[31m❌ Cannot read first frame.\033[0m", file=sys.stderr)
        sys.exit(1)

    sys.stdout.write(HIDE_CURSOR + CLEAR_SCREEN)
    sys.stdout.flush()
    splash = render_fn(first_frame, width, already_scaled=already_scaled, quantize=quantize)
    sys.stdout.write(splash)
    sys.stdout.flush()

    # ── Start pipeline ────────────────────────────────────────────────────

    fb = FrameBuffer(cap, buffer_size=120)
    fb.frame_index = 1
    fb.start()

    rp = RenderPipeline(
        fb, render_fn, width,
        already_scaled=already_scaled,
        quantize=quantize,
        buf_size=30,
    )
    rp.current_filter = filter_name
    rp.start()

    # ── Prebuffer first ───────────────────────────────────────────────────
    prebuf = 8 if is_url(str(source)) else 3
    fb.prebuffer(count=prebuf, timeout=3.0)

    # ── Audio ─────────────────────────────────────────────────────────────
    audio: AudioController | None = None
    audio_started = False
    if play_audio and not is_webcam:
        audio = AudioController(source, use_ffplay=use_ffplay)
        audio_started = audio.start()
        if audio_started and not is_url(str(source)):
            # Local file: ffplay takes about 100-150ms to start audio output.
            # Delaying the video start slightly aligns them beautifully.
            time.sleep(0.12)
        elif not audio_started:
            print("\033[33m⚠️  Warning: Background audio could not be started (is ffplay/ffmpeg missing?). Playing video only.\033[0m", file=sys.stderr)
            time.sleep(1.5)

    # ── Subtitles ─────────────────────────────────────────────────────────

    subs: list = []
    if subs_file and os.path.isfile(subs_file):
        subs = parse_srt(subs_file)

    # ── Recorder ──────────────────────────────────────────────────────────

    recorder: Recorder | None = None
    if record_file:
        recorder = Recorder(record_file)

    # ── Keyboard ──────────────────────────────────────────────────────────

    kb = KeyboardController()
    kb.start()

    # ── Playback state ────────────────────────────────────────────────────

    video_time = frame_time         # already showed frame 0
    wall_clock = time.perf_counter()
    playback_start_time = time.perf_counter()
    displayed = 1
    skipped = 0
    paused = False
    speed = 1.0
    muted = False
    show_info = True
    cur_filter_idx = FILTER_LIST.index(filter_name) if filter_name in FILTER_LIST else 0

    bar_width = max(width - 50, 10)

    seek_step = 5.0  # seconds per arrow-key press

    try:
        while True:
            # ── Handle keyboard ───────────────────────────────────────────
            key = kb.get_key()
            while key:
                if key == "quit":
                    raise KeyboardInterrupt
                elif key == "space":
                    paused = not paused
                    if paused:
                        fb._paused.clear()   # pause decoder thread
                        if audio and audio_started:
                            audio.pause()
                    elif not paused:
                        fb._paused.set()     # resume decoder thread
                        if audio and audio_started and not muted:
                            if audio.ffplay_proc is None:
                                audio.seek(video_time)
                            else:
                                audio.resume()
                    wall_clock = time.perf_counter()
                elif key == "mute":
                    if audio:
                        muted = audio.toggle_mute()
                        if not muted and not paused:
                            audio.seek(video_time)
                elif key == "volume_down":
                    if audio:
                        audio.volume_down()
                elif key == "volume_up":
                    if audio:
                        audio.volume_up()
                elif key == "filter":
                    cur_filter_idx = (cur_filter_idx + 1) % len(FILTER_LIST)
                    rp.current_filter = FILTER_LIST[cur_filter_idx]
                    rp.clear()
                elif key == "speed_down":
                    speed = max(0.25, round(speed - 0.25, 2))
                elif key == "speed_up":
                    speed = min(4.0, round(speed + 0.25, 2))
                elif key == "left" and not is_webcam:
                    new_t = max(0, video_time - seek_step)
                    new_frame = int(new_t * fps)
                    if fb.seek(new_frame):
                        rp.clear()
                        video_time = new_t
                        if audio and audio_started and not muted:
                            if paused:
                                audio._stop_playback()
                            else:
                                audio.seek(video_time)
                elif key == "right" and not is_webcam:
                    new_t = video_time + seek_step
                    if total_time > 0:
                        new_t = min(new_t, total_time - 0.1)
                    new_frame = int(new_t * fps)
                    if fb.seek(new_frame):
                        rp.clear()
                        video_time = new_t
                        if audio and audio_started and not muted:
                            if paused:
                                audio._stop_playback()
                            else:
                                audio.seek(video_time)
                elif key == "info":
                    show_info = not show_info
                key = kb.get_key()

            # ── Advance clock ─────────────────────────────────────────────
            now = time.perf_counter()
            if not paused:
                delta = now - wall_clock
                video_time += delta * speed
            wall_clock = now

            if paused:
                # Move cursor to status line
                sys.stdout.write(f"\033[{frame_lines + 1};1H")
                # Redraw status while paused
                cur_vol = audio.volume if audio else 100
                if not is_webcam:
                    status = build_progress_bar(
                        video_time, total_time, bar_width,
                        True, speed, muted, cur_vol, mode,
                        FILTER_LIST[cur_filter_idx], decoder,
                    )
                else:
                    status = build_webcam_status(0, mode, FILTER_LIST[cur_filter_idx], width)
                sys.stdout.write(status)

                # Draw subtitle line while paused
                if subs:
                    sub_text = get_current_subtitle(subs, video_time)
                    if sub_text:
                        sys.stdout.write(f"\n\033[97;44m  {sub_text}  \033[0m{ERASE_LINE}")
                    else:
                        sys.stdout.write(f"\n{ERASE_LINE}")
                else:
                    sys.stdout.write(f"\n{ERASE_LINE}")

                # Draw controls line while paused
                if show_info:
                    sys.stdout.write(f"\n{CONTROLS_LINE}{ERASE_LINE}")
                else:
                    sys.stdout.write(f"\n{ERASE_LINE}")
                sys.stdout.flush()
                time.sleep(0.05)
                continue

            # ── Check finished ────────────────────────────────────────────
            if not is_webcam and rp.is_finished:
                break

            # ── Get rendered frame ────────────────────────────────────────
            target_frame = int(video_time * fps)
            idx, ansi = rp.get_frame(target_frame)

            if ansi is None:
                if not is_webcam and rp.is_finished:
                    break
                time.sleep(0.002)
                continue

            displayed += 1

            # ── Display ───────────────────────────────────────────────────
            sys.stdout.write(ansi)

            # ── Status / progress ─────────────────────────────────────────
            sys.stdout.write(f"\033[{frame_lines + 1};1H")
            if is_webcam:
                actual_fps = displayed / max(time.perf_counter() - playback_start_time, 0.001)
                status = build_webcam_status(
                    actual_fps, mode, FILTER_LIST[cur_filter_idx], width,
                )
            else:
                cur_vol = audio.volume if audio else 100
                status = build_progress_bar(
                    video_time, total_time, bar_width,
                    False, speed, muted, cur_vol, mode,
                    FILTER_LIST[cur_filter_idx], decoder,
                )
            sys.stdout.write(status)

            # ── Subtitle ──────────────────────────────────────────────────
            if subs:
                sub_text = get_current_subtitle(subs, video_time)
                if sub_text:
                    # White on blue background, centered
                    sys.stdout.write(f"\n\033[97;44m  {sub_text}  \033[0m{ERASE_LINE}")
                else:
                    sys.stdout.write(f"\n{ERASE_LINE}")
            else:
                sys.stdout.write(f"\n{ERASE_LINE}")

            # ── Controls hint ─────────────────────────────────────────────
            if show_info:
                sys.stdout.write(f"\n{CONTROLS_LINE}{ERASE_LINE}")
            else:
                sys.stdout.write(f"\n{ERASE_LINE}")

            sys.stdout.flush()

            # ── Record ────────────────────────────────────────────────────
            if recorder:
                recorder.write_frame(ansi)

            # ── Frame pacing ──────────────────────────────────────────────
            if not is_webcam and idx is not None:
                target_wall = wall_clock + ((idx / fps - video_time) / speed if speed else 0)
                sleep_for = target_wall - time.perf_counter()
                if 0 < sleep_for < 0.1:
                    time.sleep(sleep_for)

    except KeyboardInterrupt:
        pass
    finally:
        # ── Cleanup ───────────────────────────────────────────────────────
        rp.stop()
        fb.stop_thread()
        cap.release()
        kb.stop()
        if audio:
            audio.stop()
        if recorder:
            recorder.close()

        restore_cursor()
        sys.stdout.write("\n")
        sys.stdout.flush()
        elapsed_total = video_time
        print(f"\n\033[33m👋 Playback finished.\033[0m")
        print(f"\033[90m   Displayed: {displayed} frames in {elapsed_total:.1f}s\033[0m")
        if recorder:
            print(f"\033[90m   Recorded:  {recorder.count} frames → {record_file}\033[0m")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    tw = shutil.get_terminal_size((120, 40)).columns

    parser = argparse.ArgumentParser(
        prog="ascii_player",
        description="🎬 ASCII Video Player — play videos in the terminal as colorful ASCII/ANSI art.",
        epilog=(
            "Keyboard controls during playback:\n"
            "  Space     Pause / Resume\n"
            "  ← / →     Seek ±5 seconds\n"
            "  [ / ]     Speed down / up (0.25x steps)\n"
            "  M         Mute / Unmute audio\n"
            "  F         Cycle visual filter\n"
            "  I         Toggle info bar\n"
            "  Q         Quit\n"
            "\n"
            "Examples:\n"
            "  python ascii_player.py video.mp4\n"
            "  python ascii_player.py video.mp4 --width 80 --mode ascii\n"
            "  python ascii_player.py video.mp4 --filter edge\n"
            "  python ascii_player.py video.mp4 --quality high --record out.txt\n"
            "  python ascii_player.py --webcam\n"
            '  python ascii_player.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("source", nargs="?", default=None,
                        help="Path to a video/image file or a YouTube URL.")
    parser.add_argument("--width", "-w", type=int, default=min(tw, 120),
                        help=f"Frame width in characters (default: {min(tw, 120)}).")
    parser.add_argument("--mode", "-m", choices=["block", "ascii"], default="block",
                        help="Render mode (default: block).")
    parser.add_argument("--no-audio", action="store_true",
                        help="Disable audio playback.")
    parser.add_argument("--ffplay", action="store_true",
                        help="Force ffplay for audio.")
    parser.add_argument("--decoder", choices=["opencv", "ffmpeg"], default="opencv",
                        help="Video decoder backend (default: opencv).")

    # ── New features ──────────────────────────────────────────────────────
    parser.add_argument("--webcam", type=int, nargs="?", const=0, default=None,
                        help="Webcam mode. Optionally specify camera index (default: 0).")
    parser.add_argument("--image", action="store_true",
                        help="Display a single image as ASCII art then exit.")
    parser.add_argument("--filter", choices=FILTER_LIST, default="none",
                        help="Initial visual filter (default: none).")
    parser.add_argument("--quality", choices=list(QUALITY_PRESETS.keys()), default=None,
                        help="Quality preset (overrides --width and --quantize).")
    parser.add_argument("--quantize", type=int, default=0, choices=range(0, 9),
                        help="Colour quantization level 0-8 (0 = off, higher = fewer colours).",
                        metavar="N")
    parser.add_argument("--record", type=str, default=None, metavar="FILE",
                        help="Record ANSI output to a text file.")
    parser.add_argument("--subs", type=str, default=None, metavar="FILE.srt",
                        help="SRT subtitle file to overlay.")

    return parser.parse_args()


def main() -> None:
    check_and_add_ffmpeg_to_path()
    args = parse_args()

    # Apply quality preset
    if args.quality:
        preset = QUALITY_PRESETS[args.quality]
        args.width = preset["width"]
        args.quantize = preset["quantize"]

    width = args.width
    mode = args.mode
    play_audio = not args.no_audio
    use_ffplay = args.ffplay
    decoder = args.decoder
    filter_name = args.filter
    quantize = args.quantize

    if width < 20:
        print("\033[31m❌ Width must be at least 20.\033[0m", file=sys.stderr)
        sys.exit(1)

    # ── Webcam mode ───────────────────────────────────────────────────────
    if args.webcam is not None:
        if cv2 is None:
            print("\033[31m❌ Webcam requires OpenCV.\033[0m", file=sys.stderr)
            sys.exit(1)
        print(f"\033[35m📷 Webcam Mode (camera {args.webcam})\033[0m")
        print(f"\033[90m   Press Q to exit\033[0m\n")
        play_video(
            args.webcam, width, mode,
            play_audio=False, decoder="opencv",
            filter_name=filter_name, quantize=quantize,
            is_webcam=True,
        )
        return

    # ── Image mode ────────────────────────────────────────────────────────
    if args.image:
        if not args.source:
            print("\033[31m❌ Provide an image path.\033[0m", file=sys.stderr)
            sys.exit(1)
        show_image(args.source, width, mode, filter_name, quantize)
        return

    # ── Video mode ────────────────────────────────────────────────────────
    if not args.source:
        print("\033[31m❌ Provide a video source or use --webcam.\033[0m", file=sys.stderr)
        sys.exit(1)

    source = args.source
    if is_url(source):
        if is_youtube_url(source):
            source = get_stream_url(source)
    else:
        if not os.path.isfile(source):
            print(f"\033[31m❌ File not found: {source}\033[0m", file=sys.stderr)
            sys.exit(1)

    print(f"\033[35m🎬 ASCII Video Player\033[0m")
    print(f"\033[90m   Source:   {args.source}\033[0m")
    print(f"\033[90m   Width:    {width} chars\033[0m")
    print(f"\033[90m   Mode:     {mode}\033[0m")
    print(f"\033[90m   Decoder:  {decoder}\033[0m")
    print(f"\033[90m   Filter:   {filter_name}\033[0m")
    print(f"\033[90m   Audio:    {'Enabled' if play_audio else 'Disabled'}\033[0m")
    if args.subs:
        print(f"\033[90m   Subs:     {args.subs}\033[0m")
    if args.record:
        print(f"\033[90m   Record:   {args.record}\033[0m")
    print(f"\033[90m   Press Ctrl+C or Q to exit\033[0m\n")

    play_video(
        source, width, mode,
        play_audio=play_audio, use_ffplay=use_ffplay, decoder=decoder,
        filter_name=filter_name, quantize=quantize,
        subs_file=args.subs, record_file=args.record,
    )


if __name__ == "__main__":
    main()
