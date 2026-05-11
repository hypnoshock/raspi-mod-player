#!/bin/bash

FLOPPY_DEV="/dev/sda"
FLOPPY_MOUNT="$HOME/floppy"
FALLBACK_DIR="$HOME/mod_player/mods"
PYTHON="$HOME/mod_player/.venv/bin/python"
PLAY_TRACK="$HOME/mod_player/play_track.py"
declare -g MOD_PLAYER_PID=""

last_disk_state="none"  # none, floppy, fallback

start_mod_player() {
    DIR="$1"
    echo "[+] Checking mods in: $DIR"

    # Wait for files to show up on mounted disk
    local files=()
    for i in {1..5}; do
        mapfile -t files < <(find "$DIR" -type f \
            \( -iname "*.mod" -o -iname "*.xm" -o -iname "*.s3m" -o -iname "*.it" \) \
            | shuf)
        if [ ${#files[@]} -gt 0 ]; then
            echo "[+] Found ${#files[@]} mods"
            break
        fi
        sleep 1
    done

    if [ ${#files[@]} -eq 0 ]; then
        echo "[-] No mods found in: $DIR, falling back to default directory."
        if [ "$DIR" != "$FALLBACK_DIR" ]; then
            start_mod_player "$FALLBACK_DIR"
        fi
        return
    fi

    # Loop forever through the shuffled file list, one track at a time, so
    # play_track.py can publish per-track state to /tmp/mod_state.json.
    (
        local _files=("${files[@]}")
        while true; do
            for f in "${_files[@]}"; do
                "$PYTHON" "$PLAY_TRACK" "$f"
            done
            mapfile -t _files < <(printf '%s\n' "${_files[@]}" | shuf)
        done
    ) &
    MOD_PLAYER_PID=$!
}

stop_mod_player() {
    if [ -n "$MOD_PLAYER_PID" ]; then
        echo "[-] Stopping mod player..."
        kill "$MOD_PLAYER_PID" 2>/dev/null
        pkill -P "$MOD_PLAYER_PID" 2>/dev/null
        MOD_PLAYER_PID=""
    fi
    pkill -f play_track.py 2>/dev/null
    pkill -f 'xmp -R' 2>/dev/null
}

if [ -b "$FLOPPY_DEV" ]; then
    mkdir -p "$FLOPPY_MOUNT"
    echo "attempting to mount floppy"
    sudo /bin/mount "$FLOPPY_DEV" "$FLOPPY_MOUNT"
fi

while true; do
    disk_present=0
    floppy_ready=0

    if [ -b "$FLOPPY_DEV" ]; then
        if mountpoint -q "$FLOPPY_MOUNT"; then
            if find "$FLOPPY_MOUNT" -type f \( -iname "*.mod" -o -iname "*.xm" -o -iname "*.s3m" -o -iname "*.it" \) | grep -q .; then
                disk_present=1
                floppy_ready=1
            else
                disk_present=0
                floppy_ready=0
            fi
        fi
    fi

    if [ "$floppy_ready" -eq 1 ] && [ "$last_disk_state" != "floppy" ]; then
        stop_mod_player
        start_mod_player "$FLOPPY_MOUNT"
        last_disk_state="floppy"
    fi

    if [ "$floppy_ready" -eq 0 ] && [ "$last_disk_state" == "floppy" ]; then
        stop_mod_player
        last_disk_state="none"
    fi

    if [ "$disk_present" -eq 0 ] && [ "$last_disk_state" == "none" ]; then
        start_mod_player "$FALLBACK_DIR"
        last_disk_state="fallback"
    fi

    sleep 2
done
