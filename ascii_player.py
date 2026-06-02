#!/usr/bin/env python3
"""
ASCII Video Player — CLI-утилита для воспроизведения видео в терминале
в виде цветного ASCII/ANSI-арта.

Поддерживает локальные файлы и YouTube-ссылки (через yt-dlp).
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

# Принудительно UTF-8 для stdout/stderr на Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─── Константы ────────────────────────────────────────────────────────────────

# Градиент символов от тёмного к светлому (10 уровней)
ASCII_CHARS = " .:-=+*#%@"

# Коэффициент коррекции пропорций символа в терминале
# (высота символа ~ в 2 раза больше ширины, корректируем)
CHAR_ASPECT_RATIO = 0.55

# ANSI-коды управления терминалом
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
MOVE_HOME = "\033[H"
RESET_COLOR = "\033[0m"
CLEAR_SCREEN = "\033[2J"


# ─── Утилиты ─────────────────────────────────────────────────────────────────

def is_url(path: str) -> bool:
    """Проверяет, является ли строка URL-адресом."""
    return bool(re.match(r"https?://", path, re.IGNORECASE))


def is_youtube_url(url: str) -> bool:
    """Проверяет, является ли URL ссылкой на YouTube."""
    youtube_patterns = [
        r"(https?://)?(www\.)?youtube\.com/watch",
        r"(https?://)?(www\.)?youtube\.com/shorts",
        r"(https?://)?youtu\.be/",
        r"(https?://)?(www\.)?youtube\.com/embed",
    ]
    return any(re.match(p, url, re.IGNORECASE) for p in youtube_patterns)


def get_stream_url(youtube_url: str) -> str:
    """
    Использует yt-dlp для получения прямой ссылки на видеопоток.
    Выбирает лучшее качество с разрешением ≤ 720p для производительности.
    """
    print(f"\033[33m⏳ Получаю поток из YouTube: {youtube_url}\033[0m")
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
            raise RuntimeError("yt-dlp вернул пустой URL.")
        print(f"\033[32m✅ Поток получен.\033[0m")
        return stream_url
    except FileNotFoundError:
        print(
            "\033[31m❌ yt-dlp не найден. Установите его: pip install yt-dlp\033[0m",
            file=sys.stderr,
        )
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(
            f"\033[31m❌ yt-dlp ошибка:\n{e.stderr}\033[0m",
            file=sys.stderr,
        )
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(
            "\033[31m❌ yt-dlp: превышено время ожидания (30 с).\033[0m",
            file=sys.stderr,
        )
        sys.exit(1)


# ─── Конвертация кадра в цветной ASCII ──────────────────────────────────────

def frame_to_ansi(frame: np.ndarray, width: int) -> str:
    """
    Конвертирует BGR-кадр OpenCV в строку цветного ASCII-арта
    с использованием True Color ANSI-кодов (\033[38;2;R;G;Bm).
    """
    h, w = frame.shape[:2]
    aspect = h / w
    # Вычисляем высоту с учётом пропорций символов
    new_height = int(width * aspect * CHAR_ASPECT_RATIO)
    new_width = width

    # Уменьшаем кадр до целевого размера
    resized = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)

    # Конвертируем BGR → RGB
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

    # Предвычисляем яркость для выбора символов
    # Яркость (perceived luminance) по формуле BT.601
    gray = np.dot(rgb[..., :3].astype(np.float32), [0.299, 0.587, 0.114])

    # Индексы символов (0..len-1)
    num_chars = len(ASCII_CHARS)
    indices = np.clip((gray / 255.0 * (num_chars - 1)).astype(int), 0, num_chars - 1)

    # Собираем строку посимвольно с ANSI True Color
    lines = []
    for y in range(new_height):
        row_parts = []
        prev_r, prev_g, prev_b = -1, -1, -1
        for x in range(new_width):
            r, g, b = int(rgb[y, x, 0]), int(rgb[y, x, 1]), int(rgb[y, x, 2])
            char = ASCII_CHARS[indices[y, x]]

            # Оптимизация: не повторяем escape-код, если цвет не изменился
            if r != prev_r or g != prev_g or b != prev_b:
                row_parts.append(f"\033[38;2;{r};{g};{b}m{char}")
                prev_r, prev_g, prev_b = r, g, b
            else:
                row_parts.append(char)
        lines.append("".join(row_parts))

    return RESET_COLOR + MOVE_HOME + ("\n".join(lines)) + RESET_COLOR


def frame_to_ansi_fast(frame: np.ndarray, width: int) -> str:
    """
    Оптимизированная версия конвертации — использует векторизацию NumPy
    и строковые буферы для максимальной скорости рендеринга.
    """
    h, w = frame.shape[:2]
    aspect = h / w
    new_height = int(width * aspect * CHAR_ASPECT_RATIO)
    new_width = width

    resized = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

    # Яркость
    gray = np.dot(rgb[..., :3].astype(np.float32), [0.299, 0.587, 0.114])
    num_chars = len(ASCII_CHARS)
    indices = np.clip((gray / 255.0 * (num_chars - 1)).astype(int), 0, num_chars - 1)

    # Предварительно создаём массив символов
    char_array = np.array(list(ASCII_CHARS))
    ascii_matrix = char_array[indices]  # (H, W) массив символов

    # Собираем вывод через массивы строк
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


# ─── Основной цикл воспроизведения ──────────────────────────────────────────

def play_video(source: str, width: int) -> None:
    """
    Главный цикл воспроизведения: захватывает кадры из source
    и рендерит их в терминал как цветной ASCII-арт.
    """
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"\033[31m❌ Не удалось открыть видео: {source}\033[0m", file=sys.stderr)
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 24.0  # fallback

    frame_time = 1.0 / fps
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    total_str = str(total_frames) if total_frames > 0 else "?"

    # Прячем курсор и очищаем экран один раз
    sys.stdout.write(HIDE_CURSOR + CLEAR_SCREEN)
    sys.stdout.flush()

    frame_count = 0

    try:
        while True:
            t_start = time.perf_counter()

            ret, frame = cap.read()
            if not ret:
                break  # конец видео

            frame_count += 1

            # Конвертируем и выводим кадр
            ansi_frame = frame_to_ansi_fast(frame, width)
            sys.stdout.write(ansi_frame)

            # Строка статуса
            elapsed = frame_count / fps
            mins, secs = divmod(int(elapsed), 60)
            status = (
                f"\n\033[36m▶ Кадр {frame_count}/{total_str} "
                f"| {mins:02d}:{secs:02d} "
                f"| FPS: {fps:.1f} "
                f"| Ширина: {width}\033[0m"
            )
            sys.stdout.write(status)
            sys.stdout.flush()

            # Синхронизация по FPS
            t_elapsed = time.perf_counter() - t_start
            sleep_time = frame_time - t_elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        pass  # обработка Ctrl+C — выходим чисто
    finally:
        cap.release()
        # Восстанавливаем терминал
        sys.stdout.write(SHOW_CURSOR + RESET_COLOR + "\n")
        sys.stdout.flush()
        print("\n\033[33m👋 Воспроизведение завершено.\033[0m")


# ─── Точка входа ─────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """Парсит аргументы командной строки."""
    # Автоматическое определение ширины терминала
    term_width = shutil.get_terminal_size((120, 40)).columns

    parser = argparse.ArgumentParser(
        prog="ascii_player",
        description=(
            "🎬 ASCII Video Player — воспроизведение видео в терминале "
            "в виде цветного ASCII/ANSI-арта."
        ),
        epilog="Примеры:\n"
               "  python ascii_player.py video.mp4\n"
               "  python ascii_player.py video.mp4 --width 80\n"
               '  python ascii_player.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"\n',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "source",
        help="Путь к видеофайлу или YouTube-ссылка.",
    )
    parser.add_argument(
        "--width", "-w",
        type=int,
        default=min(term_width, 120),
        help=f"Ширина ASCII-кадра в символах (по умолчанию: {min(term_width, 120)}).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.source
    width = args.width

    if width < 20:
        print("\033[31m❌ Ширина должна быть не менее 20 символов.\033[0m", file=sys.stderr)
        sys.exit(1)

    # Определяем источник
    if is_url(source):
        if is_youtube_url(source):
            source = get_stream_url(source)
        # Иначе — пробуем открыть как прямую ссылку на видео
    else:
        if not os.path.isfile(source):
            print(f"\033[31m❌ Файл не найден: {source}\033[0m", file=sys.stderr)
            sys.exit(1)

    print(f"\033[35m🎬 ASCII Video Player\033[0m")
    print(f"\033[90m   Источник: {args.source}\033[0m")
    print(f"\033[90m   Ширина:   {width} символов\033[0m")
    print(f"\033[90m   Для выхода нажмите Ctrl+C\033[0m\n")
    time.sleep(1)

    play_video(source, width)


if __name__ == "__main__":
    main()
