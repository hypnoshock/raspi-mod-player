#!/bin/bash

USB_MOUNT_BASE="/media"
FALLBACK_DIR="$HOME/mod_player/tiny"
MOD_CMD="xmp -R --loop-all"
MOD_EXTS="-iname '*.mod' -o -iname '*.xm' -o -iname '*.s3m' -o -iname '*.it'"
declare -g MOD_PLAYER_PID=""

last_disk_state="none"  # none, usb, fallback

start_mod_player() {
    DIR="$1"
    echo "[+] Checking mods in: $DIR"

    # Wait for files to show up on mounted disk
    for i in {1..5}; do
        found_files=$(find "$DIR" -type f \( -iname "*.mod" -o -iname "*.xm" -o -iname "*.s3m" -o -iname "*.it" \) -print0)
        
        if [ -n "$found_files" ]; then
            echo "[+] Found mods"
            find "$DIR" -type f \( -iname "*.mod" -o -iname "*.xm" -o -iname "*.s3m" -o -iname "*.it" \) -print0 | xargs -0 -o -- $MOD_CMD &
            MOD_PLAYER_PID=$!
            break
        fi
        
        sleep 1
    done

    # If no mods found after 5 seconds, switch to fallback
    if [ -z "$found_files" ]; then
        echo "[-] No mods found in: $DIR, falling back to default directory."
        start_mod_player "$FALLBACK_DIR"
    fi
}

stop_mod_player() {
    if [ -n "$MOD_PLAYER_PID" ]; then
        echo "[-] Stopping mod player..."
        kill "$MOD_PLAYER_PID" 2>/dev/null
        MOD_PLAYER_PID=""
    fi
    pkill -f 'xmp -R'
}

get_latest_usb_mount() {
    # Find the most recently mounted USB directory
    ls -td "$USB_MOUNT_BASE"/usb* 2>/dev/null | head -n 1
}

while true; do
    latest_usb=$(get_latest_usb_mount)

    if [ -n "$latest_usb" ]; then
        if [ "$last_disk_state" != "usb" ]; then
            stop_mod_player
            start_mod_player "$latest_usb"
            last_disk_state="usb"
        fi
    else
        if [ "$last_disk_state" == "usb" ]; then
            stop_mod_player
            last_disk_state="none"
        fi

        if [ "$last_disk_state" == "none" ]; then
            start_mod_player "$FALLBACK_DIR"
            last_disk_state="fallback"
        fi
    fi

    sleep 2
done

