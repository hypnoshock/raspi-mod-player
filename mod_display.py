#!/usr/bin/env python3
"""Render mod-player state to a 240x240 ST7789 SPI display.

Polls /tmp/mod_state.json (written by play_track.py) every ~250 ms
and draws "Now Playing", title, format, elapsed/total time, and a
progress bar.

Uses a direct spidev + lgpio driver because this specific generic
AliExpress panel only works in SPI mode 3 (clock idles HIGH), which
the Pimoroni st7789 library does not support. See
~/Documents/240x240-ips.md for the full story.
"""
import json
import os
import signal
import sys
import time

import lgpio
import numpy as np
import spidev
from PIL import Image, ImageDraw, ImageFont

STATE_PATH = '/tmp/mod_state.json'
REFRESH_INTERVAL_S = 0.125
IDLE_GRACE_S = 30

WIDTH = 240
HEIGHT = 240

FONT_DIR = '/usr/share/fonts/truetype/dejavu'
FONT_REG = os.path.join(FONT_DIR, 'DejaVuSans.ttf')
FONT_BOLD = os.path.join(FONT_DIR, 'DejaVuSans-Bold.ttf')
FONT_MONO = os.path.join(FONT_DIR, 'DejaVuSansMono.ttf')

GPIO_DC = 24
GPIO_RST = 25
GPIO_BL = 13
SPI_PORT = 0
SPI_CS = 0
SPI_SPEED_HZ = 32_000_000
SPI_MODE = 3


class ST7789:
    def __init__(self):
        self._h = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(self._h, GPIO_DC, 0)
        lgpio.gpio_claim_output(self._h, GPIO_RST, 1)
        lgpio.gpio_claim_output(self._h, GPIO_BL, 1)

        self._spi = spidev.SpiDev()
        self._spi.open(SPI_PORT, SPI_CS)
        self._spi.mode = SPI_MODE
        self._spi.max_speed_hz = SPI_SPEED_HZ

        self._init()

    def _cmd(self, c):
        lgpio.gpio_write(self._h, GPIO_DC, 0)
        self._spi.writebytes([c])

    def _data(self, d):
        lgpio.gpio_write(self._h, GPIO_DC, 1)
        if isinstance(d, int):
            self._spi.writebytes([d])
        else:
            for i in range(0, len(d), 4096):
                self._spi.writebytes2(d[i:i + 4096])

    def _init(self):
        lgpio.gpio_write(self._h, GPIO_RST, 1); time.sleep(0.05)
        lgpio.gpio_write(self._h, GPIO_RST, 0); time.sleep(0.10)
        lgpio.gpio_write(self._h, GPIO_RST, 1); time.sleep(0.15)
        self._cmd(0x01); time.sleep(0.20)              # SWRESET
        self._cmd(0x11); time.sleep(0.60)              # SLPOUT
        self._cmd(0x3A); self._data(0x55)               # COLMOD 16bpp
        self._cmd(0x36); self._data(0x00)               # MADCTL
        self._cmd(0x21)                                  # INVON
        self._cmd(0x2A); self._data([0, 0, 0, 0xEF])    # CASET 0..239
        self._cmd(0x2B); self._data([0, 0, 0, 0xEF])    # RASET 0..239
        self._cmd(0x13); time.sleep(0.02)              # NORON
        self._cmd(0x29); time.sleep(0.10)              # DISPON

    def display(self, image):
        """Push a 240x240 RGB Pillow image to the panel as RGB565 big-endian."""
        arr = np.asarray(image, dtype=np.uint8)
        r = arr[:, :, 0].astype(np.uint16)
        g = arr[:, :, 1].astype(np.uint16)
        b = arr[:, :, 2].astype(np.uint16)
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out = np.empty((HEIGHT, WIDTH, 2), dtype=np.uint8)
        out[:, :, 0] = (v >> 8).astype(np.uint8)
        out[:, :, 1] = (v & 0xFF).astype(np.uint8)
        self._cmd(0x2C)
        self._data(out.tobytes())

    def set_backlight(self, on):
        lgpio.gpio_write(self._h, GPIO_BL, 1 if on else 0)

    def shutdown(self):
        try:
            self.display(Image.new('RGB', (WIDTH, HEIGHT), (0, 0, 0)))
        except Exception:
            pass
        try:
            self.set_backlight(False)
        except Exception:
            pass


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def fmt_time(s):
    if s is None or s < 0:
        return '--:--'
    s = int(s)
    return f'{s // 60:02d}:{s % 60:02d}'


def wrap_to_width(draw, text, font, max_width):
    if not text:
        return ['']
    words = text.split(' ')
    lines = []
    cur = ''
    for w in words:
        candidate = w if not cur else cur + ' ' + w
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            if draw.textbbox((0, 0), w, font=font)[2] > max_width:
                buf = ''
                for ch in w:
                    if draw.textbbox((0, 0), buf + ch, font=font)[2] <= max_width:
                        buf += ch
                    else:
                        if buf:
                            lines.append(buf)
                        buf = ch
                cur = buf
            else:
                cur = w
    if cur:
        lines.append(cur)
    return lines


class Renderer:
    def __init__(self):
        self.display = ST7789()
        self.font_small = ImageFont.truetype(FONT_REG, 14)
        self.font_med = ImageFont.truetype(FONT_BOLD, 18)
        self.font_big = ImageFont.truetype(FONT_BOLD, 22)
        self.font_mono = ImageFont.truetype(FONT_MONO, 18)

    def draw_idle(self):
        img = Image.new('RGB', (WIDTH, HEIGHT), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text(
            (10, 100),
            'Idle',
            font=self.font_big,
            fill=(120, 120, 120),
        )
        self.display.display(img)

    def draw(self, state):
        img = Image.new('RGB', (WIDTH, HEIGHT), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        draw.text(
            (10, 8),
            'Now Playing',
            font=self.font_small,
            fill=(180, 180, 180),
        )
        draw.line([(10, 26), (WIDTH - 10, 26)], fill=(60, 60, 80), width=1)

        title = state.get('title') or os.path.basename(state.get('file', '?'))
        lines = wrap_to_width(draw, title, self.font_med, WIDTH - 20)
        y = 36
        for line in lines[:3]:
            draw.text((10, y), line, font=self.font_med, fill=(255, 220, 90))
            y += 22

        fmt = state.get('format')
        if fmt:
            fmt_lines = wrap_to_width(draw, fmt, self.font_small, WIDTH - 20)
            draw.text(
                (10, y + 6),
                fmt_lines[0],
                font=self.font_small,
                fill=(180, 180, 220),
            )

        started = state.get('started_at')
        duration = state.get('duration_s')
        elapsed = max(0.0, time.time() - started) if started else 0.0
        if duration:
            elapsed_c = min(elapsed, duration)
            t_line = f'{fmt_time(elapsed_c)} / {fmt_time(duration)}'
            frac = elapsed_c / duration if duration > 0 else 0
        else:
            t_line = fmt_time(elapsed)
            frac = None

        draw.text((10, 170), t_line, font=self.font_mono, fill=(220, 220, 220))

        bx0, by0, bx1, by1 = 10, 205, WIDTH - 10, 225
        draw.rectangle([bx0, by0, bx1, by1], outline=(80, 80, 120), width=1)
        if frac is not None and frac > 0:
            fill_x = bx0 + int((bx1 - bx0) * frac)
            if fill_x > bx0:
                draw.rectangle(
                    [bx0 + 1, by0 + 1, fill_x, by1 - 1],
                    fill=(80, 180, 255),
                )

        self.display.display(img)

    def shutdown(self):
        self.display.shutdown()


def main():
    renderer = Renderer()

    def on_term(_signum, _frame):
        renderer.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    while True:
        state = load_state()
        is_idle = False
        if state is None:
            is_idle = True
        else:
            started = state.get('started_at')
            duration = state.get('duration_s')
            if started is None:
                is_idle = True
            else:
                elapsed = time.time() - started
                if duration is not None and elapsed > duration + IDLE_GRACE_S:
                    is_idle = True
                if duration is None and elapsed > 3600:
                    is_idle = True

        try:
            if is_idle:
                renderer.draw_idle()
            else:
                renderer.draw(state)
        except Exception as e:
            sys.stderr.write(f'render error: {e}\n')
        time.sleep(REFRESH_INTERVAL_S)


if __name__ == '__main__':
    main()
