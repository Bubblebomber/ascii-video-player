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


# ─── Frame to colored ASCII conversion ──────────────────────────────────────

def frame_to_ansi(frame: np.ndarray, width: int) -> str:
    """
    Convert a BGR OpenCV frame to a colored ASCII art string
    using True Color ANSI codes (\033[38;2;R;G;Bm).
    """
    h, w = frame.shape[:2]
    aspect = h / w
    # Calculate height accounting for character aspect ratio
    new_height = int(width * aspect * CHAR_ASPECT_RATIO)
    new_width = width

    # Resize frame to target dimensions
    resized = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)

    # Convert BGR → RGB
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

    # Compute brightness for character selection
    # Perceived luminance using BT.601 formula
    gray = np.dot(rgb[..., :3].astype(np.float32), [0.299, 0.587, 0.114])

    # Character indices (0..len-1)
    num_chars = len(ASCII_CHARS)
    indices = np.clip((gray / 255.0 * (num_chars - 1)).astype(int), 0, num_chars - 1)

    # Build string character by character with ANSI True Color
    lines = []
    for y in range(new_height):
        row_parts = []
        prev_r, prev_g, prev_b = -1, -1, -1
        for x in range(new_width):
            r, g, b = int(rgb[y, x, 0]), int(rgb[y, x, 1]), int(rgb[y, x, 2])
            char = ASCII_CHARS[indices[y, x]]

            # Optimization: skip escape code if color hasn't changed
            if r != prev_r or g != prev_g or b != prev_b:
                row_parts.append(f"\033[38;2;{r};{g};{b}m{char}")
                prev_r, prev_g, prev_b = r, g, b
            else:
                row_parts.append(char)
        lines.append("".join(row_parts))

    return RESET_COLOR + MOVE_HOME + ("\n".join(lines)) + RESET_COLOR


def frame_to_ansi_fast(frame: np.ndarray, width: int) -> str:
    """
    Optimized conversion — uses NumPy vectorization
    and string buffers for maximum rendering speed.
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


# ─── Main playback loop ─────────────────────────────────────────────────────

def play_video(source: str, width: int) -> None:
    """
    Main playback loop: captures frames from source
    and renders them as colored ASCII art in the terminal.
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

    # Hide cursor and clear screen once
    sys.stdout.write(HIDE_CURSOR + CLEAR_SCREEN)
    sys.stdout.flush()

    frame_count = 0

    try:
        while True:
            t_start = time.perf_counter()

            ret, frame = cap.read()
            if not ret:
                break  # end of video

            frame_count += 1

            # Convert and render frame
            ansi_frame = frame_to_ansi_fast(frame, width)
            sys.stdout.write(ansi_frame)

            # Status bar
            elapsed = frame_count / fps
            mins, secs = divmod(int(elapsed), 60)
            status = (
                f"\n\033[36m▶ Frame {frame_count}/{total_str} "
                f"| {mins:02d}:{secs:02d} "
                f"| FPS: {fps:.1f} "
                f"| Width: {width}\033[0m"
            )
            sys.stdout.write(status)
            sys.stdout.flush()

            # FPS synchronization
            t_elapsed = time.perf_counter() - t_start
            sleep_time = frame_time - t_elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        pass  # handle Ctrl+C — exit cleanly
    finally:
        cap.release()
        # Restore terminal state
        sys.stdout.write(SHOW_CURSOR + RESET_COLOR + "\n")
        sys.stdout.flush()
        print("\n\033[33m👋 Playback finished.\033[0m")


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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.source
    width = args.width

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
    print(f"\033[90m   Press Ctrl+C to exit\033[0m\n")
    time.sleep(1)

    play_video(source, width)


if __name__ == "__main__":
    main()
