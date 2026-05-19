# mod_player

A self-contained tracker-module music player for a Raspberry Pi. Plays tracker module files through a USB audio DAC, shows the current track on a small SPI display, and accepts next/prev/seek commands from GPIO buttons, SSH, or (eventually) a web UI.

Built around `libopenmpt` directly — no CLI player, no screen-scraping, no virtual keyboard tricks.

## Supported formats

| Extension | Format |
|-----------|--------|
| .mod | ProTracker MOD |
| .xm | FastTracker 2 Extended Module |
| .it | Impulse Tracker |
| .s3m | Scream Tracker 3 |
| .mptm | OpenMPT |
| .mtm | MultiTracker |
| .mo3 | MO3 (compressed module with MP3/Ogg samples) |
| .mt2 | MadTracker 2 |
| .med | OctaMED |
| .ams | Extreme's Tracker / Velvet Studio |
| .dbm | Digi Booster Pro |
| .dmf | ASYLUM Music Format / X-Tracker |
| .dtm | Digital Tracker / Digital Home Studio |
| .ult | UltraTracker |
| .symmod | Symphonie / Symphonie Pro |
| .okt | Oktalyzer |

## Hardware

This is what's wired up on the reference build. Other configurations work but the install script and pin assignments assume this layout.

| Component | Connection |
|---|---|
| Raspberry Pi | Tested on Pi Zero W (Bookworm, 32-bit). Should work on any Pi with SPI and a USB host. |
| 240×240 ST7789 SPI display (no CS pin) | Wired per `docs/240x240-ips.md` — SCL→GPIO11, SDA→GPIO10, RES→GPIO25, DC→GPIO24, BLK→GPIO13, VCC→3V3, GND→GND. SPI mode 3. |
| Next button | GPIO 26 to GND (active-low, internal pull-up) |
| Previous button | GPIO 6 to GND (active-low, internal pull-up) |
| Audio output | Any USB audio class device. Reference build uses a Burr-Brown USB DAC. |
| Module library | Drop tracker files into `~/mod_player/mods/` |
| (Optional) USB floppy / mass-storage | Hot-plugs as `/dev/sda`. When present and contains tracker files, the daemon switches to it; falls back to `mods/` when removed. |

## How it works

```
                ┌─────────────────────────────────────────────────┐
                │ mod_playerd.py        (mod_playerd.service)     │
                │                                                 │
   GPIO 6/26 ──▶│  buttons (gpiozero, lgpio backend)              │
   (buttons)    │   ─▶ ("next"/"prev",) on command queue          │
                │                                                 │
   /tmp/        │  control socket (ThreadingUnixStreamServer)     │
   modplayer ──▶│   ─▶ next/prev/pause/resume/seek/status/quit    │
   .sock        │                                                 │
                │  playlist (~/floppy if /dev/sda else            │
                │   ~/mod_player/mods; shuffle, wrap, reshuffle)  │
                │                                                 │
                │  Module (ctypes → libopenmpt.so.0)              │
                │   renderer thread ─▶ int16 PCM ─▶ audio queue   │
                │   audio thread    ─▶ sounddevice → ALSA direct  │
                │                       │                         │
                │                       ▼                         │
                │              USB Audio CODEC                    │
                │                                                 │
                │  1Hz tick ─▶ writes /tmp/mod_state.json         │
                └─────────────────────────────────────────────────┘
                                          │
                                          ▼
                            ┌────────────────────────────────┐
                            │ mod_display.py                 │
                            │ (mod_display.service)          │
                            │  - polls /tmp/mod_state.json   │
                            │  - renders a Pillow image      │
                            │  - pushes it to ST7789 over SPI│
                            └────────────────────────────────┘
                                          │
                                          ▼
                                  240×240 IPS panel
```

Two systemd services:

- **`mod_playerd.service`** — single Python process that owns everything: it loads modules via `libopenmpt` (ctypes), renders PCM into `sounddevice`, reads the GPIO buttons, listens on a Unix socket for commands, and writes the current track + playhead to `/tmp/mod_state.json` once a second.
- **`mod_display.service`** — independent Python daemon that polls the JSON state file and draws to the ST7789. Knows nothing about playback; only about pixels.

The two are connected only by the state file, so each can be restarted without touching the other.

## Files

| File | Purpose |
|---|---|
| `mod_playerd.py` | Playback daemon. The thing that makes noise and listens for input. |
| `openmpt.py` | Minimal `ctypes` wrapper around `libopenmpt.so.0`. |
| `playlist.py` | Source state machine — floppy vs. fallback dir — and shuffle. |
| `modctl` | Tiny CLI client that talks to `/tmp/modplayer.sock`. |
| `mod_display.py` | Display daemon — Pillow rendering + an inline ST7789 driver. |
| `services/*.service` | systemd unit templates (`__USER__` / `__HOME__` placeholders, substituted by the installer). |
| `install.sh` | One-shot installer for a fresh Pi. |
| `docs/DISPLAY-FEATURE.md` | Deeper architecture notes for the display side. |
| `docs/240x240-ips.md` | Hardware/wiring reference for the display panel. |
| `docs/DRIVER-NOTES.md` | Why we ship our own ST7789 driver instead of using Pimoroni's. |
| `mods/` | Where your tracker files live. |

## Installing on a fresh Raspberry Pi

Assuming a Pi Zero W (or any Pi with SPI + USB), starting from nothing:

1. **Flash Raspberry Pi OS Bookworm Lite** (32-bit). Use the Imager's pre-config to set hostname, enable SSH, set the user/password, and pre-load Wi-Fi credentials. The script and units assume the username is whatever you logged in as.

2. **Wire the hardware** as in the table above. Double-check the SPI display wiring against `docs/240x240-ips.md` — that panel uses SPI mode 3 and has a few non-obvious pin quirks.

3. **Boot the Pi and SSH in**, then clone this repo:
   ```bash
   git clone <this-repo> ~/mod_player
   cd ~/mod_player
   ```

4. **Run the installer**:
   ```bash
   sudo ./install.sh
   ```
   It will:
   - install apt packages (`libopenmpt0`, `portaudio19-dev`, `python3-venv` + the various `python3-*` deps for the display, `fonts-dejavu-core`)
   - enable SPI in `/boot/firmware/config.txt` if needed
   - add your user to the `audio`, `gpio`, and `spi` groups
   - drop a `sudoers.d` entry letting your user run `mount` / `umount` without a password (for the USB floppy feature)
   - mount `/tmp` as `tmpfs` (RAM-backed) — see "SD card longevity" below for why
   - create a Python venv at `~/mod_player/.venv` with `--system-site-packages` and pip-install `sounddevice` into it
   - write `mod_playerd.service` and `mod_display.service` to `/etc/systemd/system/` with your username baked in
   - symlink `modctl` into `/usr/local/bin/`
   - enable and (if no reboot was needed) start both services
   - disable the legacy `gpio_keypress.service` if it's hanging around from an older install

5. **If the installer enabled SPI**, reboot:
   ```bash
   sudo reboot
   ```
   After reboot the services start automatically.

6. **Drop some tracker files** into `~/mod_player/mods/` and confirm:
   ```bash
   modctl status
   ```
   should answer with a JSON line describing the current track. The display should show the title + progress bar. Pressing the buttons should advance / go back.

## Using it

`modctl` is the control surface — runs from anywhere on the Pi (or remotely over SSH):

```
modctl status        # JSON: current file, title, format, elapsed, paused, etc.
modctl next          # skip forward
modctl prev          # skip back
modctl pause
modctl resume
modctl seek +10      # +10 s
modctl seek -5       # -5 s
modctl seek 60       # absolute, to 1:00
modctl quit          # stops the daemon (systemd will restart it)
```

The display follows automatically — no separate update needed. Buttons fire the same `next` / `prev` commands internally.

To plug in a USB stick formatted with tracker files, just plug it in — within ~2 seconds the daemon switches playback to its contents. Unplug to go back to `~/mod_player/mods/`.

## ⚠️ SD card longevity — keep `/tmp` on tmpfs

The daemon writes `/tmp/mod_state.json` roughly 10 times a second so the display can render fresh pattern/playhead data. Raspberry Pi OS Bookworm does **not** mount `/tmp` as tmpfs by default — so by default, those writes hit the SD card. That's a few hundred MB of writes per day for state that has zero reason to survive a reboot.

`install.sh` mitigates this by adding a line to `/etc/fstab`:
```
tmpfs  /tmp  tmpfs  defaults,nosuid,nodev,size=64M  0  0
```
After the next reboot, `/tmp` is RAM-backed. No SD card writes from this project. If you ever clone this repo onto a new Pi and run `install.sh`, you'll get this for free; if you have an older install that pre-dates this change, just add the line manually and reboot. To check whether `/tmp` is currently tmpfs:
```bash
findmnt /tmp
```
should show `SOURCE=tmpfs`. If it shows nothing, the mount isn't active — confirm the fstab line and reboot.

## Logs and troubleshooting

```bash
# Daemon
journalctl -u mod_playerd -f
sudo systemctl restart mod_playerd

# Display
journalctl -u mod_display -f
sudo systemctl restart mod_display

# State file the display reads
cat /tmp/mod_state.json
```

If `modctl` says `cannot reach daemon`, the daemon isn't running — check the journal. If you get audio glitches under heavy CPU, the easy knob is `FRAMES_PER_CHUNK` in `mod_playerd.py` (raise it to e.g. 2048).

For display hardware oddities, `docs/240x240-ips.md` and `docs/DRIVER-NOTES.md` cover the painful lessons learned bringing the panel up.

## Removing the legacy install

Older revisions of this project used `xmp` running inside a `screen` session driven by virtual keyboard events. If you're upgrading from that, the installer handles the systemd side, but you can also delete the now-unused files once you've soaked the new daemon:

```
rm ~/mod_player/{mod_monitor.sh,mod_monitor.sh.bak.*,play_track.py,input.py}
rm ~/input.py  # the old install.sh copied input.py to $HOME too
sudo rm /etc/systemd/system/gpio_keypress.service
```

And remove the `if [ -z "$SSH_CLIENT" ]; then sleep 5; screen -dmS player ...` block from `~/.bashrc` if it's still there.
