#!/usr/bin/env python3
"""
ASCII Video Player — CLI utility for playing videos in the terminal
as colorful ASCII/ANSI art.

Supports local files and YouTube links (via yt-dlp).
"""

import argparse
import sys
import time
import os
import re
import subprocess
import shutil
import io
import threading
import collections

import cv2
import numpy as np

# Force UTF-8 for stdout/stderr on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─── Constants ────────────────────────────────────────────────────────────────

# Character gradient from dark to bright (10 levels)
ASCII_CHARS = " .:-=+*#%@"

# Aspect ratio correction for terminal characters
# (character height is ~2x its width, we compensate)
CHAR_ASPECT_RATIO = 0.55

# Unicode half-block character — top half filled
# Used to pack 2 vertical pixels per character cell:
#   foreground color = top pixel, background color = bottom pixel
HALF_BLOCK = "▀"

# ANSI terminal control codes
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
MOVE_HOME = "\033[H"
RESET_COLOR = "\033[0m"
CLEAR_SCREEN = "\033[2J"


# ─── Utilities ────────────────────────────────────────────────────────────────

def is_url(path: str) -> bool:
    """Check if the string is a URL."""
    return bool(re.match(r"https?://", path, re.IGNORECASE))


def is_youtube_url(url: str) -> bool:
    """Check if the URL is a YouTube link."""
    youtube_patterns = [
        r"(https?://)?(www\.)?youtube\.com/watch",
        r"(https?://)?(www\.)?youtube\.com/shorts",
        r"(https?://)?youtu\.be/",
        r"(https?://)?(www\.)?youtube\.com/embed",
    ]
    return any(re.match(p, url, re.IGNORECASE) for p in youtube_patterns)


def get_stream_url(youtube_url: str) -> str:
    """
    Use yt-dlp to get the direct video stream URL.
    Selects best quality at ≤720p for performance.
    """
    print(f"\033[33m⏳ Fetching stream from YouTube: {youtube_url}\033[0m")
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "yt_dlp",
                "--format", "best[height<=720]",
                "--get-url",
                youtube_url,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        stream_url = result.stdout.strip()
        if not stream_url:
            raise RuntimeError("yt-dlp returned an empty URL.")
        print(f"\033[32m✅ Stream URL obtained.\033[0m")
        return stream_url
    except FileNotFoundError:
        print(
            "\033[31m❌ yt-dlp not found. Install it: pip install yt-dlp\033[0m",
            file=sys.stderr,
        )
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(
            f"\033[31m❌ yt-dlp error:\n{e.stderr}\033[0m",
            file=sys.stderr,
        )
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(
            "\033[31m❌ yt-dlp: timeout exceeded (30s).\033[0m",
            file=sys.stderr,
        )
        sys.exit(1)


# ─── Frame buffer (pre-buffering + background decoding) ─────────────────────

class FrameBuffer:
    """
    Background thread that continuously reads frames from the video source
    and stores them in a ring buffer. This decouples decoding from rendering,
    eliminates initial stutter, and allows frame skipping for real-time sync.
    """

    def __init__(self, cap: cv2.VideoCapture, buffer_size: int = 60):
        self.cap = cap
        self.buffer = collections.deque(maxlen=buffer_size)
        self.lock = threading.Lock()
        self.frame_index = 0       # next frame index to be read
        self.finished = False
        self._thread = threading.Thread(target=self._reader, daemon=True)

    def start(self):
        self._thread.start()

    def _reader(self):
        while True:
            # Rate limit the producer: block/sleep if the buffer is full
            while True:
                with self.lock:
                    if len(self.buffer) < self.buffer.maxlen:
                        break
                time.sleep(0.005)

            ret, frame = self.cap.read()
            if not ret:
                self.finished = True
                break
            with self.lock:
                self.buffer.append((self.frame_index, frame))
                self.frame_index += 1

    def prebuffer(self, count: int = 10, timeout: float = 5.0):
        """Wait until at least `count` frames are buffered or timeout."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            with self.lock:
                if len(self.buffer) >= count or self.finished:
                    return
            time.sleep(0.01)

    def get_latest(self):
        """
        Return the most recent frame and its index, discarding older ones.
        This enables frame skipping for real-time sync.
        Returns (frame_index, frame) or (None, None) if empty.
        """
        with self.lock:
            if not self.buffer:
                return None, None
            idx, frame = self.buffer[-1]
            self.buffer.clear()
            return idx, frame

    def get_next(self):
        """
        Return the oldest buffered frame (sequential playback).
        Returns (frame_index, frame) or (None, None) if empty.
        """
        with self.lock:
            if not self.buffer:
                return None, None
            return self.buffer.popleft()

    @property
    def is_finished(self):
        with self.lock:
            return self.finished and len(self.buffer) == 0


# ─── Frame to colored ASCII conversion ──────────────────────────────────────

def frame_to_ansi_fast(frame: np.ndarray, width: int) -> str:
    """
    Optimized conversion — uses NumPy vectorization
    and string buffers for maximum rendering speed.
    Uses ASCII characters colored with True Color ANSI.
    """
    h, w = frame.shape[:2]
    aspect = h / w
    new_height = int(width * aspect * CHAR_ASPECT_RATIO)
    new_width = width

    resized = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

    # Brightness
    gray = np.dot(rgb[..., :3].astype(np.float32), [0.299, 0.587, 0.114])
    num_chars = len(ASCII_CHARS)
    indices = np.clip((gray / 255.0 * (num_chars - 1)).astype(int), 0, num_chars - 1)

    # Pre-build character array
    char_array = np.array(list(ASCII_CHARS))
    ascii_matrix = char_array[indices]  # (H, W) character array

    # Build output via string arrays
    buf = [MOVE_HOME]
    for y in range(new_height):
        row_rgb = rgb[y]        # (W, 3)
        row_chars = ascii_matrix[y]  # (W,)
        prev_color = None
        parts = []
        for x in range(new_width):
            r, g, b = int(row_rgb[x, 0]), int(row_rgb[x, 1]), int(row_rgb[x, 2])
            color = (r, g, b)
            ch = row_chars[x]
            if color != prev_color:
                parts.append(f"\033[38;2;{r};{g};{b}m{ch}")
                prev_color = color
            else:
                parts.append(ch)
        buf.append("".join(parts))

    return RESET_COLOR + "\n".join(buf) + RESET_COLOR


def frame_to_halfblock(frame: np.ndarray, width: int) -> str:
    """
    High-quality conversion using Unicode half-block characters (▀).
    Each terminal cell displays TWO vertical pixels:
      - foreground color (top pixel)
      - background color (bottom pixel)
    This effectively doubles vertical resolution compared to ASCII mode.
    """
    h, w = frame.shape[:2]
    aspect = h / w

    # Target height in PIXELS (not characters) — we pack 2 rows per cell
    # so we need an even number of pixel rows
    pixel_height = int(width * aspect)
    if pixel_height % 2 != 0:
        pixel_height += 1
    char_height = pixel_height // 2

    resized = cv2.resize(frame, (width, pixel_height), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

    buf = [MOVE_HOME]
    for cy in range(char_height):
        top_row = rgb[cy * 2]       # (W, 3) — top pixel row
        bot_row = rgb[cy * 2 + 1]   # (W, 3) — bottom pixel row
        parts = []
        prev_fg = None
        prev_bg = None
        for x in range(width):
            # Top pixel → foreground color
            fr, fg_, fb = int(top_row[x, 0]), int(top_row[x, 1]), int(top_row[x, 2])
            # Bottom pixel → background color
            br, bg_, bb = int(bot_row[x, 0]), int(bot_row[x, 1]), int(bot_row[x, 2])

            cur_fg = (fr, fg_, fb)
            cur_bg = (br, bg_, bb)

            # Build minimal escape sequence
            ansi_seq = ""
            if cur_fg != prev_fg:
                ansi_seq += f"\033[38;2;{fr};{fg_};{fb}m"
                prev_fg = cur_fg
            if cur_bg != prev_bg:
                ansi_seq += f"\033[48;2;{br};{bg_};{bb}m"
                prev_bg = cur_bg
            parts.append(f"{ansi_seq}{HALF_BLOCK}")

        buf.append("".join(parts))

    return RESET_COLOR + "\n".join(buf) + RESET_COLOR


# ─── Main playback loop ─────────────────────────────────────────────────────

def play_video(source: str, width: int, mode: str = "block") -> None:
    """
    Main playback loop: captures frames from source
    and renders them as colored art in the terminal.

    Uses background frame buffering and real-time sync
    with frame skipping to prevent stuttering.
    """
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"\033[31m❌ Failed to open video: {source}\033[0m", file=sys.stderr)
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 24.0  # fallback

    frame_time = 1.0 / fps
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    total_str = str(total_frames) if total_frames > 0 else "?"

    # Select render function
    render_fn = frame_to_halfblock if mode == "block" else frame_to_ansi_fast

    # Read the first frame immediately for the splash screen
    ret, first_frame = cap.read()
    if not ret:
        print(f"\033[31m❌ Failed to read first frame from: {source}\033[0m", file=sys.stderr)
        sys.exit(1)

    # Hide cursor and clear screen immediately to show splash frame
    sys.stdout.write(HIDE_CURSOR + CLEAR_SCREEN)
    sys.stdout.flush()

    splash_ansi = render_fn(first_frame, width)
    sys.stdout.write(splash_ansi)
    sys.stdout.write(f"\n\033[33m⏳ Buffering video stream... Please wait...\033[0m")
    sys.stdout.flush()

    # Start background frame reader and pre-buffer
    # Since we already read frame index 0, we tell FrameBuffer to start from index 1.
    fb = FrameBuffer(cap, buffer_size=120)
    fb.frame_index = 1
    fb.start()
    fb.prebuffer(count=20, timeout=5.0)

    # We start the loop. We already showed frame index 0.
    displayed = 1
    skipped = 0
    playback_start = time.perf_counter() - frame_time

    try:
        while not fb.is_finished:
            now = time.perf_counter()
            elapsed = now - playback_start

            # Determine which frame we SHOULD be showing right now
            target_frame = int(elapsed * fps)

            # Skip/discard frames that are older than target_frame
            while True:
                with fb.lock:
                    if not fb.buffer:
                        break
                    next_idx, _ = fb.buffer[0]
                
                if next_idx < target_frame:
                    with fb.lock:
                        fb.buffer.popleft()
                    skipped += 1
                else:
                    break

            # Now pop the oldest frame to display
            idx, frame = None, None
            with fb.lock:
                if fb.buffer:
                    idx, frame = fb.buffer.popleft()

            if frame is None:
                if fb.finished:
                    break
                # Buffer underrun — wait/buffer in background, and pause/adjust playback_start
                underrun_start = time.perf_counter()
                while not fb.finished:
                    with fb.lock:
                        if fb.buffer:
                            break
                    time.sleep(0.005)
                underrun_duration = time.perf_counter() - underrun_start
                playback_start += underrun_duration
                continue

            displayed += 1

            # Convert and render frame
            ansi_frame = render_fn(frame, width)
            sys.stdout.write(ansi_frame)

            # Status bar
            current_time = (idx + 1) / fps if idx is not None else (displayed / fps)
            mins, secs = divmod(int(current_time), 60)
            frame_label = idx + 1 if idx is not None else displayed
            actual_fps = displayed / elapsed if elapsed > 0 else 0
            status = (
                f"\n\033[36m▶ Frame {frame_label}/{total_str} "
                f"| {mins:02d}:{secs:02d} "
                f"| FPS: {actual_fps:.1f}/{fps:.0f} "
                f"| {mode.upper()} "
                f"| W:{width}\033[0m"
            )
            sys.stdout.write(status)
            sys.stdout.flush()

            # Wait until the target display time for this frame
            expected_display_time = playback_start + (idx * frame_time if idx is not None else displayed * frame_time)
            sleep_time = expected_display_time - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        pass  # handle Ctrl+C — exit cleanly
    finally:
        cap.release()
        # Restore terminal state
        sys.stdout.write(SHOW_CURSOR + RESET_COLOR + "\n")
        sys.stdout.flush()
        total_elapsed = time.perf_counter() - playback_start
        print(f"\n\033[33m👋 Playback finished.\033[0m")
        print(f"\033[90m   Displayed: {displayed} frames in {total_elapsed:.1f}s\033[0m")
        if skipped > 0:
            print(f"\033[90m   Skipped:   {skipped} frames (real-time sync)\033[0m")


# ─── Entry point ─────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    # Auto-detect terminal width
    term_width = shutil.get_terminal_size((120, 40)).columns

    parser = argparse.ArgumentParser(
        prog="ascii_player",
        description=(
            "🎬 ASCII Video Player — play videos in the terminal "
            "as colorful ASCII/ANSI art."
        ),
        epilog="Examples:\n"
               "  python ascii_player.py video.mp4\n"
               "  python ascii_player.py video.mp4 --width 80\n"
               "  python ascii_player.py video.mp4 --mode ascii\n"
               '  python ascii_player.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"\n',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "source",
        help="Path to a video file or a YouTube URL.",
    )
    parser.add_argument(
        "--width", "-w",
        type=int,
        default=min(term_width, 120),
        help=f"ASCII frame width in characters (default: {min(term_width, 120)}).",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["block", "ascii"],
        default="block",
        help="Render mode: 'block' (high-quality half-block pixels) "
             "or 'ascii' (classic ASCII characters). Default: block.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.source
    width = args.width
    mode = args.mode

    if width < 20:
        print("\033[31m❌ Width must be at least 20 characters.\033[0m", file=sys.stderr)
        sys.exit(1)

    # Determine source
    if is_url(source):
        if is_youtube_url(source):
            source = get_stream_url(source)
        # Otherwise — try opening as a direct video URL
    else:
        if not os.path.isfile(source):
            print(f"\033[31m❌ File not found: {source}\033[0m", file=sys.stderr)
            sys.exit(1)

    print(f"\033[35m🎬 ASCII Video Player\033[0m")
    print(f"\033[90m   Source: {args.source}\033[0m")
    print(f"\033[90m   Width:  {width} chars\033[0m")
    print(f"\033[90m   Mode:   {mode}\033[0m")
    print(f"\033[90m   Press Ctrl+C to exit\033[0m\n")

    play_video(source, width, mode)


if __name__ == "__main__":
    main()
