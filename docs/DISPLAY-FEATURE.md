# Display feature — how it works

The Pi has a 240×240 SPI display showing the currently playing tracker module: title, format, elapsed/total time and a progress bar. This document explains the pieces and how they fit together.

## Architecture

```
                       ┌──────────────────────────┐
   GPIO 6 / 26  ──────▶│ input.py                 │──▶ uinput keys ──▶ tty1
   (buttons)           │ (gpio_keypress.service)  │
                       └──────────────────────────┘

   tty1 autologin ──▶ ~/.bashrc
                       │
                       └─▶ screen -dmS player mod_monitor.sh
                                                  │
                                                  ▼
                            ┌────────────────────────────────┐
                            │ mod_monitor.sh                  │
                            │  - finds .mod/.xm/.s3m/.it      │
                            │  - shuffles the list            │
                            │  - calls play_track.py FILE     │
                            │    for each file in a loop      │
                            └────────────────────────────────┘
                                          │
                                          ▼
                            ┌────────────────────────────────┐
                            │ play_track.py FILE              │
                            │  - spawns `xmp -R FILE` in a    │
                            │    pseudo-tty (pty.fork)        │
                            │  - fans xmp's UI back to the    │
                            │    real terminal                │
                            │  - forwards stdin keypresses    │
                            │    to xmp so buttons keep       │
                            │    working                      │
                            │  - parses the first ~8 KB of    │
                            │    xmp's output for the         │
                            │    module header                │
                            │  - writes /tmp/mod_state.json   │
                            └────────────────────────────────┘
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

`gpio_keypress.service` and the audio pipeline (`xmp` → ALSA/PulseAudio) are untouched by the display feature; the display reads state asynchronously and has no path back into playback.

## play_track.py — getting state out of xmp

`xmp` is a CLI tracker player. It prints module metadata to stdout on startup and otherwise runs an interactive UI in the terminal. We need two things from it:

1. Per-track metadata (title, format, duration) so the display can show useful info and draw a correct progress bar.
2. To keep its interactive UI intact in the screen session — so the user can still see what xmp is doing on the framebuffer console, and so the UInput keystrokes from `input.py` still reach it.

The wrapper accomplishes both by giving xmp a **pseudo-tty** (`pty.fork()`):

- The child process becomes `xmp -R FILE` with its stdin/stdout/stderr connected to the slave end of the pty.
- The parent (us) reads from the master end in a `select` loop:
  - Anything xmp writes is forwarded to our own stdout (so the screen session keeps seeing xmp's UI).
  - The same bytes are accumulated in a small parse buffer and regex-matched for the lines xmp prints early in playback:
    - `Module name  : <title>`
    - `Module type  : <format>`
    - `Duration     : NminSSs`
- The parent also reads stdin and writes it to the pty so button-driven UInput keystrokes still drive xmp.

Each time any of the three fields is parsed, the wrapper writes (atomically via `tmp+rename`) a JSON state file:

```
/tmp/mod_state.json
{
  "file":        "/home/hypnoshock/mod_player/mods/abandoned_highway.xm",
  "title":       "abandoned highway",
  "format":      "FastTracker v2.00 XM 1.04",
  "duration_s":  157,
  "started_at":  1715432104.31
}
```

`started_at` is set at process start (wall-clock seconds, fractional). The display computes elapsed time as `now - started_at` rather than scraping xmp's running status line — xmp's status line uses cursor-movement ANSI codes and is fragile to parse, whereas wall-clock is plenty accurate for a progress bar.

The wrapper exits when xmp exits, and the outer `for f in files; do play_track.py "$f"; done` loop in `mod_monitor.sh` moves on to the next track. The bash loop re-shuffles after each full pass.

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

Effective refresh: about 4–5 fps. Plenty for a clock + progress bar; doesn't load the CPU enough to interfere with `xmp` playback.

## Files involved

| File                              | Purpose                                      |
|-----------------------------------|----------------------------------------------|
| `mod_monitor.sh`                  | Disk watcher + per-track playback loop       |
| `play_track.py`                   | xmp wrapper that emits `/tmp/mod_state.json` |
| `mod_display.py`                  | Display daemon (local ST7789 driver + UI)    |
| `mod_display.service`             | systemd unit — runs `mod_display.py` on boot |
| `docs/240x240-ips.md`             | Display hardware/wiring reference            |
| `docs/DRIVER-NOTES.md`            | Why we don't use the Pimoroni library        |
| `docs/DISPLAY-FEATURE.md`         | This file                                    |
