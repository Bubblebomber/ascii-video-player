# 🎬 ASCII Video Player

> Play videos right in your terminal — as **colorful ASCII/ANSI art** with True Color support.

<p align="center">
  <img src="https://img.shields.io/badge/python-3.8+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.8+"/>
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT License"/>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-informational?style=for-the-badge" alt="Platform"/>
</p>

---

## ✨ Features

- 🎥 **Local files** — MP4, AVI, MKV, MOV, and any format supported by OpenCV/FFmpeg
- 🌐 **YouTube** — paste a link, `yt-dlp` extracts the stream automatically
- 🎨 **True Color** — full-color output via ANSI escape codes (`\033[38;2;R;G;Bm`)
- ⚡ **Flicker-free** — rendering by cursor repositioning, not screen clearing
- 🔄 **FPS sync** — playback at the original video framerate
- 📐 **Auto aspect ratio** — correct proportions accounting for terminal character shape
- 🛡️ **Clean exit** — `Ctrl+C` gracefully restores cursor and terminal state

---

## 📦 Installation

```bash
# Clone the repository
git clone https://github.com/Bubblebomber/ascii-video-player.git
cd ascii-video-player

# Install dependencies
pip install -r requirements.txt
```

### Dependencies

| Package | Purpose |
|---------|---------|
| `opencv-python` | Video frame capture and decoding |
| `numpy` | Fast pixel array operations |
| `yt-dlp` | YouTube stream extraction |

> **Recommended:** install [FFmpeg](https://ffmpeg.org/) for maximum video format compatibility.

---

## 🚀 Usage

### Local video file

```bash
python ascii_player.py video.mp4
```

### YouTube video

```bash
python ascii_player.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

### Custom width

```bash
python ascii_player.py video.mp4 --width 80
```

### Help

```bash
python ascii_player.py --help
```

---

## ⚙️ CLI Options

| Option | Short | Description | Default |
|--------|-------|-------------|---------|
| `source` | — | Path to video file or YouTube URL | *(required)* |
| `--width` | `-w` | ASCII frame width in characters | `120` (or terminal width) |

---

## 🧠 How It Works

```
┌──────────────┐     ┌──────────┐     ┌──────────────┐     ┌──────────────┐
│   Source     │────▸│  OpenCV   │────▸│  Conversion   │────▸│  Terminal    │
│  (file/URL)  │     │  Decoder  │     │  ASCII+ANSI   │     │  Renderer   │
└──────────────┘     └──────────┘     └──────────────┘     └──────────────┘
       │                                      │
       ▼                                      ▼
  yt-dlp (if                Brightness → char from " .:-=+*#%@"
  YouTube URL)              RGB → ANSI True Color \033[38;2;R;G;Bm
```

1. **Source** — if a YouTube link is provided, `yt-dlp` extracts the direct stream URL (≤720p)
2. **Decoding** — `cv2.VideoCapture` reads frames from the file or stream
3. **Scaling** — each frame is resized to terminal width; height is calculated with a `0.55` coefficient to compensate for character aspect ratio
4. **ASCII conversion** — pixel brightness (BT.601) determines the character, RGB is encoded via True Color ANSI
5. **Rendering** — frame is output in a single `sys.stdout.write` call; cursor returns to home via `\033[H` — no flickering
6. **FPS** — `time.perf_counter` + `time.sleep` synchronize playback speed

---

## 🖥️ Terminal Requirements

Your terminal must support **True Color (24-bit)** for correct color display:

| Terminal | Support |
|----------|---------|
| Windows Terminal | ✅ |
| iTerm2 | ✅ |
| GNOME Terminal | ✅ |
| Alacritty | ✅ |
| Kitty | ✅ |
| cmd.exe (Windows) | ⚠️ Limited |

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).

---

<p align="center">
  Made with ❤️ and ANSI escape codes
</p>
