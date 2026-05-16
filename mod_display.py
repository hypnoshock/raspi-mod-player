#!/usr/bin/env python3
"""Render mod-player state to a 240x240 ST7789 SPI display.

Reads /tmp/mod_state.json (written by mod_playerd.py) and renders
"Now Playing" title/format/time/progress bar plus a faint scrolling
pattern view in the background.

Pipeline notes:
  - The main UI (title, format, time text, progress bar) is cached in a
    numpy frame buffer. It only re-renders when one of those fields
    actually changes — most frames just reuse the cache.
  - Pattern rows are pre-rendered into small grayscale "tile" images
    keyed by the row's channel-string tuple, then tinted to colour via
    numpy multiply. Each unique row formats through Pillow exactly once
    per track. The per-frame work for the pattern background becomes 17
    numpy slice-copies.
  - SPI is pushed at 64 MHz (above the 62.5 MHz datasheet figure for
    ST7789; works on this panel in practice — drop to 32 MHz here if
    you see pixel corruption).

ST7789 driver lives inline because this generic AliExpress panel only
works in SPI mode 3 (clock idles HIGH), which the Pimoroni library
hard-codes against. See ~/Documents/240x240-ips.md for the saga.
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
SPI_SPEED_HZ = 64_000_000   # bumped from 32MHz for higher refresh — drop back if you see pixel corruption
SPI_MODE = 3

# Pattern background look-and-feel.
PATTERN_FONT_PX = 11
PATTERN_ROW_HEIGHT = 13
PATTERN_COLOR_DIM = (60, 30, 100)
PATTERN_COLOR_CURRENT = (150, 100, 220)
# Sub-pixel smooth scrolling between row changes. Off by default — the
# discrete row-jump look reads more like a real tracker. Flip to True to
# slide the pattern continuously using row_start_at / row_duration_s from
# the daemon's state file.
PATTERN_SMOOTH_SCROLL = False

# fps log cadence — every N seconds, print average. Very cheap.
FPS_LOG_INTERVAL_S = 10.0


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

    def push_rgb565(self, rgb565_bytes):
        """Push pre-converted RGB565 bytes straight to the panel."""
        self._cmd(0x2C)
        self._data(rgb565_bytes)

    def set_backlight(self, on):
        lgpio.gpio_write(self._h, GPIO_BL, 1 if on else 0)

    def shutdown(self):
        try:
            # Push a black RGB565 frame (all zeros == black).
            self.push_rgb565(bytes(HEIGHT * WIDTH * 2))
        except Exception:
            pass
        try:
            self.set_backlight(False)
        except Exception:
            pass


def rgb565_be(c):
    """Pack one (R, G, B) tuple into a (hi, lo) byte pair, big-endian
    RGB565 — the panel's native format."""
    r, g, b = c
    return (
        ((r & 0xF8) | (g >> 5)) & 0xFF,
        (((g & 0x1C) << 3) | (b >> 3)) & 0xFF,
    )


def solid_565_tile(h, w, c):
    """A solid-colour (h, w, 2) uint8 tile in big-endian RGB565."""
    hi, lo = rgb565_be(c)
    t = np.empty((h, w, 2), dtype=np.uint8)
    t[:, :, 0] = hi
    t[:, :, 1] = lo
    return t


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
        self.font_pattern = ImageFont.truetype(FONT_MONO, PATTERN_FONT_PX)

        # Frame buffer is in the panel's native RGB565 big-endian layout —
        # (H, W, 2) uint8 means we push self._frame.tobytes() straight to
        # SPI with no per-frame bit math. The conversion happens once
        # inside each cached tile instead of once per frame.
        self._frame = np.zeros((HEIGHT, WIDTH, 2), dtype=np.uint8)
        self._row_tile_cache = {}   # tuple[str,...] -> (H, W, 2) uint8 RGB565
        # Tile width keyed by channel count, so every row of a given track
        # composites at the same pixels even when individual glyphs nudge
        # the natural textbbox by a pixel either way.
        self._row_tile_w_by_n = {}

        # Stamp caches — static (rebuilds on track change), dynamic (per
        # second tick).  Each is a list of (y, x, rgb_tile) triples.
        self._static_key = None
        self._static_stamps = []
        self._dynamic_key = None
        self._dynamic_stamps = []

    # --- pattern tile cache ------------------------------------------------

    def _get_row_tiles(self, row_tuple):
        """Returns (dim_565, current_565) — both pre-tinted uint8 (H, W, 2)
        in big-endian RGB565. Cached per unique row tuple. Drawing reduces
        to a memcpy into the frame buffer (which is already RGB565)."""
        cached = self._row_tile_cache.get(row_tuple)
        if cached is not None:
            return cached
        text = ' '.join(cell or '...' for cell in row_tuple)
        # Width is keyed by channel count and measured once with a worst-case
        # 3-char placeholder (format_cell_note always returns 3-char strings
        # like "C-5" or "---", so 'XXX' is an upper bound). All rows of the
        # same module composite at identical pixel positions — measuring per
        # actual row content gave ±1 px drift and a numpy broadcast error
        # when _draw_pattern tried to fit a 208 px tile into a 207 px slot.
        n = max(1, len(row_tuple))
        tile_w = self._row_tile_w_by_n.get(n)
        if tile_w is None:
            placeholder = ' '.join('XXX' for _ in range(n))
            tmp = Image.new('L', (WIDTH, PATTERN_ROW_HEIGHT), 0)
            bbox = ImageDraw.Draw(tmp).textbbox(
                (0, 0), placeholder, font=self.font_pattern)
            tile_w = min(WIDTH, bbox[2] - bbox[0] + 2)
            self._row_tile_w_by_n[n] = tile_w
        img = Image.new('L', (tile_w, PATTERN_ROW_HEIGHT), 0)
        ImageDraw.Draw(img).text((0, 0), text, font=self.font_pattern, fill=255)
        alpha = np.asarray(img, dtype=np.uint16)        # (H, W) 0..255
        # Pre-tint into RGB565 directly. This was the work we used to do per
        # frame across the whole 240x240 buffer; doing it once per unique row
        # tile is dramatically cheaper.
        def tint(c):
            sr = (alpha * c[0] // 255).astype(np.uint8)
            sg = (alpha * c[1] // 255).astype(np.uint8)
            sb = (alpha * c[2] // 255).astype(np.uint8)
            t = np.empty((PATTERN_ROW_HEIGHT, tile_w, 2), dtype=np.uint8)
            t[:, :, 0] = (sr & 0xF8) | (sg >> 5)
            t[:, :, 1] = ((sg & 0x1C) << 3) | (sb >> 3)
            return t
        tiles = (tint(PATTERN_COLOR_DIM), tint(PATTERN_COLOR_CURRENT))
        if len(self._row_tile_cache) > 1024:
            self._row_tile_cache.clear()
        self._row_tile_cache[row_tuple] = tiles
        return tiles

    def _draw_pattern(self, pattern):
        rows = pattern.get('rows') or []
        if not rows:
            return
        current_idx = pattern.get('current_idx', len(rows) // 2)

        # Optional sub-pixel smooth scroll between row changes (see
        # PATTERN_SMOOTH_SCROLL at top of file). When off, each row update
        # snaps the pattern up by exactly PATTERN_ROW_HEIGHT pixels.
        if PATTERN_SMOOTH_SCROLL:
            row_start_at = pattern.get('row_start_at')
            row_duration_s = pattern.get('row_duration_s') or 0.0
            if row_start_at and row_duration_s > 0:
                progress = (time.time() - row_start_at) / row_duration_s
                progress = max(0.0, min(1.0, progress))
            else:
                progress = 0.0
            sub_offset = int(progress * PATTERN_ROW_HEIGHT)
        else:
            sub_offset = 0

        first = self._get_row_tiles(tuple(rows[0]))
        tile_w = first[0].shape[1]
        x0 = max(0, (WIDTH - tile_w) // 2)
        y_centre = HEIGHT // 2 - PATTERN_FONT_PX // 2
        for i, row in enumerate(rows):
            tiles = first if i == 0 else self._get_row_tiles(tuple(row))
            y = y_centre + (i - current_idx) * PATTERN_ROW_HEIGHT - sub_offset
            if y + PATTERN_ROW_HEIGHT <= 0 or y >= HEIGHT:
                continue
            ty0 = max(0, -y)
            ty1 = min(PATTERN_ROW_HEIGHT, HEIGHT - y)
            fy0 = max(0, y)
            fy1 = fy0 + (ty1 - ty0)
            # Current row brighter — based on visual position, not data index,
            # so the highlight tracks the smooth scroll.
            is_current = (i == current_idx)
            tile = tiles[1] if is_current else tiles[0]
            self._frame[fy0:fy1, x0:x0 + tile_w, :] = tile[ty0:ty1]

    # --- UI: split into static (header/title/format) and dynamic (time/bar) ---
    #
    # Each is rendered as a list of "stamps" — small (y, x, rgb_tile)
    # rectangles. Compositing is then a sequence of slice-copies, no
    # full-frame mask. Static stamps only rebuild on title/format change.
    # Dynamic stamps rebuild on every second-tick / progress-bar pixel.

    def _render_text_stamp(self, text, font, fill):
        """Render a string to a tightly-cropped (h, w, 2) uint8 RGB565 stamp."""
        # Measure first.
        tmp = Image.new('L', (WIDTH, 50), 0)
        d = ImageDraw.Draw(tmp)
        bbox = d.textbbox((0, 0), text, font=font)
        w = max(1, bbox[2])
        h = max(1, bbox[3])
        img = Image.new('L', (w, h), 0)
        ImageDraw.Draw(img).text((0, 0), text, font=font, fill=255)
        alpha = np.asarray(img, dtype=np.uint16)
        sr = (alpha * fill[0] // 255).astype(np.uint8)
        sg = (alpha * fill[1] // 255).astype(np.uint8)
        sb = (alpha * fill[2] // 255).astype(np.uint8)
        out = np.empty((h, w, 2), dtype=np.uint8)
        out[:, :, 0] = (sr & 0xF8) | (sg >> 5)
        out[:, :, 1] = ((sg & 0x1C) << 3) | (sb >> 3)
        return out

    def _build_static_stamps(self, state):
        stamps = []
        header = 'Shuffling' if state.get('shuffling') else 'Now Playing'
        pos = state.get('playlist_pos')
        total = state.get('playlist_total')
        if pos and total:
            header = f'{header}  {pos} / {total}'
        stamps.append((8, 10, self._render_text_stamp(
            header, self.font_small, (180, 180, 180))))
        # Magenta 'F' in the top-right corner while the USB stick is the
        # active source, so it's obvious at a glance which tree the
        # playlist is coming from.
        if state.get('source') == 'floppy':
            f_stamp = self._render_text_stamp(
                'F', self.font_pattern, (255, 0, 255))
            stamps.append((10, WIDTH - f_stamp.shape[1] - 6, f_stamp))
        # Divider line — a thin rectangle.
        line = solid_565_tile(1, WIDTH - 20, (60, 60, 80))
        stamps.append((26, 10, line))

        title = state.get('title') or os.path.basename(state.get('file', '?'))
        # Measure with a transient draw to wrap. Done once per cache miss.
        tmp = ImageDraw.Draw(Image.new('L', (WIDTH, HEIGHT), 0))
        lines = wrap_to_width(tmp, title, self.font_med, WIDTH - 20)
        y = 36
        for line_text in lines[:3]:
            stamps.append((y, 10, self._render_text_stamp(
                line_text, self.font_med, (255, 220, 90))))
            y += 22

        fmt = state.get('format')
        if fmt:
            fmt_lines = wrap_to_width(tmp, fmt, self.font_small, WIDTH - 20)
            stamps.append((y + 6, 10, self._render_text_stamp(
                fmt_lines[0], self.font_small, (180, 180, 220))))
        return stamps

    def _build_dynamic_stamps(self, state):
        stamps = []
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

        stamps.append((170, 10, self._render_text_stamp(
            t_line, self.font_mono, (220, 220, 220))))

        # Progress-bar outline (1-px frame). Compose as four thin rects.
        bx0, by0, bx1, by1 = 10, 205, WIDTH - 10, 225
        bw = bx1 - bx0 + 1
        bh = by1 - by0 + 1
        outline_colour = (80, 80, 120)
        top = solid_565_tile(1, bw, outline_colour)
        bot = solid_565_tile(1, bw, outline_colour)
        lft = solid_565_tile(bh, 1, outline_colour)
        rgt = solid_565_tile(bh, 1, outline_colour)
        stamps.append((by0, bx0, top))
        stamps.append((by1, bx0, bot))
        stamps.append((by0, bx0, lft))
        stamps.append((by0, bx1, rgt))

        if frac is not None and frac > 0:
            fill_x = bx0 + int((bx1 - bx0) * frac)
            if fill_x > bx0:
                fill = solid_565_tile(bh - 2, fill_x - bx0, (80, 180, 255))
                stamps.append((by0 + 1, bx0 + 1, fill))
        return stamps

    def _ensure_stamps(self, state):
        static_key = (state.get('title'), state.get('format'),
                      state.get('file'), state.get('source'),
                      bool(state.get('shuffling')),
                      state.get('playlist_pos'), state.get('playlist_total'))
        if static_key != self._static_key:
            self._static_stamps = self._build_static_stamps(state)
            self._static_key = static_key

        started = state.get('started_at') or 0.0
        duration = state.get('duration_s') or 0
        elapsed = max(0.0, time.time() - started)
        if duration:
            frac_px = int(min(elapsed, duration) / duration * (WIDTH - 22))
        else:
            frac_px = 0
        dynamic_key = (int(elapsed), frac_px, duration)
        if dynamic_key != self._dynamic_key:
            self._dynamic_stamps = self._build_dynamic_stamps(state)
            self._dynamic_key = dynamic_key

    def _apply_stamps(self, stamps):
        for y, x, tile in stamps:
            h, w = tile.shape[0], tile.shape[1]
            y2 = min(HEIGHT, y + h)
            x2 = min(WIDTH, x + w)
            self._frame[y:y2, x:x2] = tile[:y2 - y, :x2 - x]

    # --- compose + push ----------------------------------------------------

    def _compose_and_push(self, pattern):
        self._frame.fill(0)
        if pattern is not None:
            self._draw_pattern(pattern)
        self._apply_stamps(self._static_stamps)
        self._apply_stamps(self._dynamic_stamps)
        self.display.push_rgb565(self._frame.tobytes())

    def draw(self, state):
        self._ensure_stamps(state)
        self._compose_and_push(state.get('pattern'))

    def draw_idle(self):
        self._frame.fill(0)
        stamp = self._render_text_stamp('Idle', self.font_big, (120, 120, 120))
        self._frame[100:100 + stamp.shape[0], 10:10 + stamp.shape[1]] = stamp
        self.display.push_rgb565(self._frame.tobytes())
        # Invalidate caches so re-entering playback redraws fresh.
        self._static_key = None
        self._dynamic_key = None

    def shutdown(self):
        self.display.shutdown()


def main():
    renderer = Renderer()

    def on_term(_signum, _frame):
        renderer.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    frames = 0
    last_report = time.monotonic()
    # Redraw only when the daemon has rewritten /tmp/mod_state.json. The
    # daemon now suppresses no-op writes, so this mtime-poll is a cheap
    # signal that something visible changed. IDLE_POLL_S caps how long
    # the display can lag a real change.
    last_mtime = -1
    # Poll fast enough that we never miss a row tick. os.stat on tmpfs is
    # ~1 microsecond — the work is in the redraw, which is gated by an
    # actual mtime change, so this is essentially free CPU-wise.
    IDLE_POLL_S = 0.04
    while True:
        try:
            mtime = os.stat(STATE_PATH).st_mtime_ns
        except FileNotFoundError:
            mtime = 0
        if mtime == last_mtime:
            time.sleep(IDLE_POLL_S)
            continue
        last_mtime = mtime

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
        frames += 1
        now = time.monotonic()
        if now - last_report >= FPS_LOG_INTERVAL_S:
            fps = frames / (now - last_report)
            sys.stderr.write(f'[mod_display] {fps:.1f} fps avg\n')
            sys.stderr.flush()
            frames = 0
            last_report = now


if __name__ == '__main__':
    main()
