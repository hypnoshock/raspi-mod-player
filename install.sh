#!/usr/bin/env bash
# mod_player installer — sets up a fresh Raspberry Pi from scratch.
# Run with: sudo ./install.sh
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Run with sudo: sudo $0"
    exit 1
fi

if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    REAL_USER="$SUDO_USER"
else
    echo "Could not determine the non-root user. Run as 'sudo $0', not as root directly."
    exit 1
fi
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPECTED_DIR="$REAL_HOME/mod_player"

if [ "$SCRIPT_DIR" != "$EXPECTED_DIR" ]; then
    echo "This installer expects the repo at $EXPECTED_DIR but it's at $SCRIPT_DIR"
    echo "Move/clone the repo to $EXPECTED_DIR and re-run."
    exit 1
fi

run_as_user() { sudo -u "$REAL_USER" "$@"; }

echo "=> installing apt packages"
apt-get update
apt-get install -y \
    libopenmpt0 \
    portaudio19-dev \
    python3-venv \
    python3-pip \
    python3-numpy \
    python3-pil \
    python3-spidev \
    python3-lgpio \
    python3-gpiozero \
    fonts-dejavu-core

CONFIG=/boot/firmware/config.txt
if [ ! -f "$CONFIG" ]; then
    CONFIG=/boot/config.txt  # pre-Bookworm path
fi
NEED_REBOOT=0
if ! grep -qE '^\s*dtparam=spi=on' "$CONFIG"; then
    echo "=> enabling SPI in $CONFIG"
    echo 'dtparam=spi=on' >> "$CONFIG"
    NEED_REBOOT=1
fi

echo "=> adding $REAL_USER to audio, gpio, spi groups"
for grp in audio gpio spi; do
    if getent group "$grp" >/dev/null; then
        usermod -aG "$grp" "$REAL_USER"
    fi
done

SUDOERS_FILE=/etc/sudoers.d/mod_player-floppy
if [ ! -f "$SUDOERS_FILE" ]; then
    echo "=> granting $REAL_USER passwordless mount/umount for the floppy feature"
    printf '%s ALL=(ALL) NOPASSWD: /bin/mount, /bin/umount\n' "$REAL_USER" > "$SUDOERS_FILE"
    chmod 0440 "$SUDOERS_FILE"
fi

# /tmp on tmpfs — the daemon writes /tmp/mod_state.json ~10 Hz so the
# display sees fresh pattern data. Raspberry Pi OS doesn't tmpfs /tmp by
# default; without this, every state write hits the SD card.
TMPFS_LINE='tmpfs  /tmp  tmpfs  defaults,nosuid,nodev,size=64M  0  0'
if ! grep -qE '^\s*tmpfs\s+/tmp\s' /etc/fstab; then
    echo "=> adding tmpfs mount for /tmp to /etc/fstab (takes effect on reboot)"
    echo "$TMPFS_LINE" >> /etc/fstab
    NEED_REBOOT=1
fi

VENV="$REAL_HOME/mod_player/.venv"
if [ ! -d "$VENV" ]; then
    echo "=> creating Python venv at $VENV (with system site packages)"
    run_as_user python3 -m venv --system-site-packages "$VENV"
fi
echo "=> installing sounddevice into the venv"
run_as_user "$VENV/bin/pip" install --upgrade --quiet sounddevice

echo "=> ensuring directories exist"
run_as_user mkdir -p "$REAL_HOME/mod_player/mods" "$REAL_HOME/floppy"

echo "=> installing modctl into /usr/local/bin"
chmod +x "$SCRIPT_DIR/modctl"
ln -sf "$SCRIPT_DIR/modctl" /usr/local/bin/modctl

echo "=> installing systemd units from services/"
for unit in mod_playerd.service mod_display.service; do
    src="$SCRIPT_DIR/services/$unit"
    if [ ! -f "$src" ]; then
        echo "missing $src — repo is incomplete"
        exit 1
    fi
    sed -e "s|__USER__|$REAL_USER|g" -e "s|__HOME__|$REAL_HOME|g" \
        "$src" > "/etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable mod_playerd.service mod_display.service

# Legacy cleanup — leftover from the xmp-based install.
if systemctl list-unit-files | grep -q '^gpio_keypress.service'; then
    echo "=> disabling legacy gpio_keypress.service"
    systemctl disable --now gpio_keypress.service 2>/dev/null || true
fi

if [ "$NEED_REBOOT" -eq 1 ]; then
    echo
    echo "SPI was just enabled in $CONFIG — reboot to take effect:"
    echo "  sudo reboot"
    echo "After reboot, both services will start automatically."
else
    echo "=> starting services"
    systemctl restart mod_playerd.service mod_display.service
    sleep 3
    if systemctl is-active --quiet mod_playerd.service; then
        echo
        echo "Installed and running. Try:"
        echo "  modctl status"
    else
        echo
        echo "mod_playerd.service is not running — check: sudo journalctl -u mod_playerd -n 50"
        exit 1
    fi
fi
