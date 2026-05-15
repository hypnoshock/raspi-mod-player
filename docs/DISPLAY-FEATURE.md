# Display feature — how it works

The Pi has a 240×240 SPI display showing the currently playing tracker module: title, format, elapsed/total time and a progress bar. This document explains the pieces and how they fit together.

## Architecture

```
                ┌─────────────────────────────────────────────────┐
                │ mod_playerd.py        (mod_playerd.service)     │
                │                                                  │
   GPIO 6/26 ──▶│  buttons (gpiozero, lgpio backend)              │
   (buttons)    │   ─▶ ("next"/"prev",) on command queue          │
                │                                                  │
   /tmp/        │  control socket (ThreadingUnixStreamServer)     │
   modplayer ──▶│   ─▶ next/prev/pause/resume/seek/status/quit    │
   .sock        │                                                  │
                │  playlist (~/floppy if /dev/sda else            │
                │   ~/mod_player/mods; shuffle, wrap, reshuffle)  │
                │                                                  │
                │  Module (ctypes → libopenmpt.so.0)              │
                │   renderer thread ─▶ int16 PCM ─▶ audio queue   │
                │   audio thread ─▶ sounddevice (RawOutputStream) │
                │                       │                          │
                │                       ▼                          │
                │              ALSA direct (USB Audio CODEC)      │
                │                                                  │
                │  1Hz tick ─▶ writes /tmp/mod_state.json         │
                └─────────────────────────────────────────────────┘
                                          │
                                          ▼
                            ┌────────────────────────────────┐
                            │ mod_display.py                  │
                            │ (mod_display.service)           │
                            │  - polls /tmp/mod_state.json    │
                            │  - renders a Pillow image       │
                            │  - pushes it to ST7789 over SPI │
                            └────────────────────────────────┘
                                          │
                                          ▼
                                  240×240 IPS panel
```

The display reads state asynchronously and has no path back into playback. To drive playback from anywhere else (SSH, a future web UI), talk to the daemon over `/tmp/modplayer.sock` — the bundled `modctl` CLI is a thin client (e.g. `modctl next`, `modctl seek +10`, `modctl status`).

## mod_playerd.py — state for the display

The daemon writes (atomically via `tmp+rename`) `/tmp/mod_state.json` on every track change and on a 1 Hz tick:

```
{
  "file":        "/home/hypnoshock/mod_player/mods/abandoned_highway.xm",
  "title":       "abandoned highway",
  "format":      "FastTracker 2 v1.04",
  "duration_s":  157,
  "elapsed_s":   42.3,
  "started_at":  1715432104.31,
  "paused":      false,
  "source":      "fallback"
}
```

`elapsed_s` is the authoritative playhead — read straight from `libopenmpt`'s position, so seeks (via socket or future web UI) reflect immediately. `started_at` is back-derived each tick as `time.time() - elapsed_s` so the display's existing wall-clock arithmetic (`elapsed = now - started_at`) keeps working unchanged. When paused, `elapsed_s` stops advancing and `started_at` drifts forward at the same rate, so the bar effectively freezes between ticks.

The display ignores any fields it doesn't know — future additions (visualiser data, queue, etc.) are non-breaking.

## mod_display.py — drawing the screen

A standalone Python daemon, run by `mod_display.service`. Single loop:

```
forever:
  state = load /tmp/mod_state.json
  if state is missing or "stale" (more than duration + 30 s past started_at):
    draw idle screen
  else:
    draw track screen
  sleep 125 ms
```

### Render pipeline

For each frame:

1. **Allocate** a 240×240 Pillow RGB image filled with black.
2. **Draw** with `PIL.ImageDraw`:
   - "Now Playing" header in small grey
   - A horizontal divider line
   - Track title (wrapped to fit the width, up to 3 lines) in yellow bold
   - Format string in small lilac
   - Elapsed / total time as `MM:SS / MM:SS` in monospace
   - Progress bar: blue fill, fraction = `elapsed / duration`
3. **Push** the image to the ST7789 via the local driver (next section).

Fonts come from `/usr/share/fonts/truetype/dejavu/` (DejaVu Sans, Sans-Bold, Sans-Mono) which ship with `python3-pil`.

If `started_at` is in the past beyond `duration_s + 30 s`, or if there's no `started_at`, the display switches to an "Idle" screen so the panel doesn't keep showing a stale track between songs / when nothing's playing.

### Pushing pixels — the local ST7789 driver

`mod_display.py` contains a small `ST7789` class implemented directly on `spidev` + `lgpio`. Why we don't use the Pimoroni `st7789` package: see `DRIVER-NOTES.md`. The short answer is that this panel only works in SPI mode 3 and that library hard-codes mode 0.

What the local driver does:

- Claims `GPIO 24` (DC), `GPIO 25` (RST), `GPIO 13` (BL) as outputs via `lgpio`.
- Opens `/dev/spidev0.0` at SPI mode **3**, **32 MHz**.
- On `__init__`, runs a minimal ST7789 init sequence (hard reset → SWRESET → SLPOUT → COLMOD 0x55 → MADCTL 0x00 → INVON → CASET 0..239 → RASET 0..239 → NORON → DISPON).
- `display(image)` converts the Pillow `RGB` image to RGB565 big-endian using numpy (vectorised — ~20 ms on a Pi Zero; pure-Python would be ~2 s) and writes it after a single `RAMWR` command.
- `shutdown()` blanks the screen and turns off the backlight; called from SIGTERM/SIGINT handlers so stopping the service leaves the panel cleanly off.

### Timing budget on a Pi Zero

| Step                                  | ~time per frame |
|---------------------------------------|-----------------|
| Pillow draw (text + rectangles)       | 25–40 ms        |
| RGB888 → RGB565 (numpy)               | 15–25 ms        |
| SPI transfer of 115 KB at 32 MHz      | ~29 ms          |
| `sleep(0.125)`                         | 125 ms          |
| **Total per loop**                    | ~200–220 ms     |

Effective refresh: about 4–5 fps. Plenty for a clock + progress bar; doesn't load the CPU enough to interfere with playback.

## Files involved

| File                              | Purpose                                              |
|-----------------------------------|------------------------------------------------------|
| `mod_playerd.py`                  | Playback daemon — owns libopenmpt + GPIO + socket    |
| `openmpt.py`                      | ctypes wrapper around `libopenmpt.so.0`              |
| `playlist.py`                     | Floppy/fallback source state machine + shuffle       |
| `modctl`                          | Unix-socket CLI client (next/prev/seek/status/...)   |
| `mod_playerd.service`             | systemd unit — runs `mod_playerd.py` on boot         |
| `mod_display.py`                  | Display daemon (local ST7789 driver + UI)            |
| `mod_display.service`             | systemd unit — runs `mod_display.py` on boot         |
| `docs/240x240-ips.md`             | Display hardware/wiring reference                    |
| `docs/DRIVER-NOTES.md`            | Why we don't use the Pimoroni library                |
| `docs/DISPLAY-FEATURE.md`         | This file                                            |
