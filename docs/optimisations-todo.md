# Display performance — optimisations TODO

A grab-bag of ideas for pushing `mod_display.py` past its current ~15 fps ceiling on the Pi Zero W, ordered roughly by effort vs reward. Captured the day we got the scrolling pattern feature working; not something to do urgently, but worth keeping in one place for when 15 fps isn't enough.

## Where the budget goes today

At ~15 fps we're spending ~66 ms per frame. Measured breakdown:

| Stage | Cost | Already optimal? |
|---|---:|---|
| State JSON parse | ~2 ms | yes |
| Pattern tile composite (numpy slice-copies) | ~5 ms | yes — Pillow is bypassed, tile cache hits |
| UI stamps composite | ~2 ms | yes |
| `RGB888 → RGB565` conversion | ~19 ms | **no** |
| SPI write (115 KB @ 64 MHz) | ~28 ms | **bandwidth-limited** |
| Misc (frame zero, dict lookups, GC, JSON dump on daemon side) | ~10 ms | partly |

The two real targets are the **RGB565 conversion** and the **SPI write**. Everything else is either trivial or already as good as it gets in this design.

**Frame drops** are a separate concern — those aren't average-budget problems, they're tail-latency problems. Almost certainly the Python GC pausing, or the audio render thread holding the GIL longer occasionally during a libopenmpt pattern transition. Different fix (see below).

## Optimisations that don't require leaving Python

| Idea | Saving | Effort |
|---|---:|---|
| **Native RGB565 throughout.** Tiles cached as 2-byte pixels, frame buffer is uint16, no final conversion step. Cleanest single win. | ~19 ms | 1 day |
| **Partial-region SPI updates.** ST7789 supports `CASET`/`RASET` to set a write window. Almost every frame only the pattern strip changes — the header/title/format rows are static. Push just the changed strip. | ~10–15 ms | half day |
| **Overlap SPI with next frame's CPU.** Hand the SPI write off to a thread, start composing the next frame immediately. They run concurrently — display latency is doubled, but throughput doubles too. | ~20 ms hidden | half day, with care |
| **Preallocate / disable GC during render.** `gc.disable()` + manual `gc.collect()` during quiet windows. Eliminates pause-glitches without changing throughput. | fewer dropped frames | a few hours |
| **CPU governor to `performance`.** `cpufreq-set -g performance`. Pi Zero defaults to `ondemand` which scales clock down when "idle" — but our load is bursty so we sometimes get throttled mid-render. | 5–10 % headroom | 5 minutes |

Stack everything above and you're somewhere around **30 fps stable** on the current Pi Zero W. That's the practical Python ceiling on this hardware.

## The C / Zig question

**Honest answer: probably overkill, but it would work.**

The reason it's overkill: the hot path is already running in C — `numpy.ndarray.__setitem__`, `spidev.writebytes2`, the bit math in `rgb_to_rgb565_bytes`. The Python interpreter is orchestrating, not doing pixel-level work. So porting to C/Zig wouldn't 100× the parts that are actually slow; it would 10× the orchestration parts and give us maybe ~50 fps. But the SPI transfer is still 28 ms regardless of language — that's a hardware floor.

That said, if you wanted to do it:

- **Zig is a nice fit** for this kind of thing — small standalone binary, manual memory, can talk to `/dev/spidev0.0` and `/dev/gpiochip0` directly via ioctls, no runtime to start up. It'd be a few hundred lines.
- **C is the safer pick** if you want lots of reference material — `spidev` is well-documented, the ST7789 panel init is unchanged, and you can keep the same `/tmp/mod_state.json` contract or switch to a small binary shared-memory page.
- **What it would buy you**: probably 40–60 fps achievable. More room for the visualiser later. Less GIL drama with the audio side. Lower CPU = less heat.
- **What you'd lose**: the ergonomics of rapid iteration. The current `mod_display.py` is ~450 lines and you can edit-restart in 3 seconds. A Zig port would be a 2-day project plus an ongoing maintenance cost — you'd want a build step on the Pi.

## The other direction: kernel-level framebuffer

Linux can expose the ST7789 as a `/dev/fb1` framebuffer via the **fbtft** kernel module (or its newer DRM-based replacement, `tinydrm`). If that works on this panel — and it might, despite the SPI-mode-3 quirk — you write pixels to `/dev/fb1` like any framebuffer and the kernel handles the SPI push. That means:

- Pixel writes are `mmap`'d memory writes — sub-microsecond per pixel.
- The kernel can do DMA-driven SPI, freeing the CPU entirely during the transfer.
- No more 28 ms SPI bottleneck (well, DMA still takes time, but the *CPU* doesn't wait for it).
- Pillow / numpy / anything can draw into the framebuffer; you don't need bespoke pixel pushing.

This is probably the single biggest win available. It's also the least certain — depends on getting fbtft to recognise the panel with the SPI-mode-3 quirk, which would mean either patching the module or finding a compatible device-tree overlay. A weekend project, with risk.

## Hardware path (cheapest)

A **Pi Zero 2 W** is pin-compatible with what you have and gives you 4× ARMv7 cores at higher clock. Everything we've done here would essentially "just work" at 4× the speed — 60 fps probably comfortable. Costs £15–20 and a microSD swap. This is the path of least pain if 60 fps is the goal.

## Suggested order

1. **CPU governor to `performance`** (5 min, free). Try this first; might already kill the frame drops.
2. **GC tuning** for the drops specifically (couple of hours). Cheap and won't hurt.
3. **Native RGB565 pipeline** (1 day). Gets you to ~22–25 fps comfortably.
4. **Partial SPI updates** (half day). Gets you to ~30 fps.
5. If 30 fps still isn't enough, **swap to a Pi Zero 2 W** before reaching for Zig/C — same code, 4× the room.
6. Only consider rewriting the display in C/Zig if you've outgrown both #4 and #5 and want it as a deliberate fun project rather than a need.

The **fbtft / tinydrm** angle is the wildcard — could be the biggest single win, but only if the panel cooperates with an existing driver.
