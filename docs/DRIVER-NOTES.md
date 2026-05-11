# Display driver workaround

## TL;DR

This project doesn't use the Pimoroni `st7789` library at runtime, even though it's installed in the venv. `mod_display.py` ships its own minimal ST7789 driver written directly on top of `spidev` + `lgpio`.

The reason: the specific 240×240 IPS panel we're using only responds in **SPI mode 3** (clock idles HIGH). Pimoroni's library hard-codes mode 0, and Adafruit/CircuitPython variants do the same. Patching the library is fragile because reinstalling wipes the change.

See `240x240-ips.md` in this folder for the full hardware/timing story and the mode reference table.

## What was tried

1. **`pip install st7789` then call it directly.** Constructed in mode 0, the panel ignored every command. Backlight worked; no pixels.
2. **Sweep through SPI modes 0/1/2/3 with manual init.** Modes 2 and 3 rendered pixels; modes 0 and 1 gave a black screen. This is what surfaced the mode-3 requirement.
3. **Subclass the Pimoroni `ST7789`, let parent init in mode 0, then switch `spi.mode = 3` and call `_init()` again.** Did not work — Pimoroni's `_init()` does extensive panel-specific setup (MADCTL=0x70, COLMOD=0x05, full gamma + power control, **no `sleep` after `SLPOUT`**). Even when re-run in mode 3, the panel didn't render. We didn't dig further because the next option was simpler.
4. **Direct-driver class inside `mod_display.py`** — chosen. Mirrors exactly the init sequence proved working during bring-up: SWRESET → SLPOUT (600 ms wait) → COLMOD 0x55 → MADCTL 0x00 → INVON → CASET/RASET → NORON → DISPON, all in SPI mode 3.

## Current runtime settings (in `mod_display.py`)

| Setting              | Value      |
|----------------------|------------|
| GPIO DC              | 24 (pin 18)|
| GPIO RST             | 25 (pin 22)|
| GPIO BL              | 13 (pin 33)|
| SPI bus / CE         | 0 / 0      |
| SPI mode             | **3**      |
| SPI clock            | 32 MHz     |
| Refresh interval     | 125 ms     |
| Pixel format         | RGB565 BE  |

Pillow image → RGB565 conversion is done with numpy (vectorised) — pure-Python conversion is ~2 s per 240×240 frame on a Pi Zero, numpy brings it under 20 ms.

## If a future panel ever supports the Pimoroni library

The library is still in the venv and works fine in mode 0 on a "normal" ST7789 board. If you swap to a Pimoroni-branded display or another panel that survives mode 0, you can drop the local `ST7789` class in `mod_display.py` and use `from st7789 import ST7789` directly.

## Useful debug references (kept for next time something doesn't render)

- `ls /dev/spidev0.*` — confirms `dtparam=spi=on` took effect
- `gpioinfo gpiochip0` — pin function/owner
- `sudo cat /sys/kernel/debug/pinctrl/*pinctrl-bcm2835*/pinmux-pins` — confirms GPIO 9/10/11 are in `alt0` (owned by SPI peripheral)
- SPI loopback test (MOSI ↔ MISO via a single jumper, send pattern, read back) — proves SPI hardware end-to-end
- GPIO-via-MISO test (jumper GPIO-under-test to pin 21, toggle, read MISO via `spi.xfer2`) — proves a single GPIO pin actually drives its line
- Never stack two female-female dupont sockets onto a single Pi header pin — the second one rides on the first one's friction and intermittently loses contact. This caused us hours of grief.
