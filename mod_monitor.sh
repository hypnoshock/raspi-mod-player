#!/bin/bash

FLOPPY_DEV="/dev/sda"
FLOPPY_MOUNT="$HOME/floppy"
FALLBACK_DIR="$HOME/mod_player/tiny"
MOD_CMD="xmp -R --loop-all"
MOD_EXTS="-iname '*.mod' -o -iname '*.xm' -o -iname '*.s3m' -o -iname '*.it'"
declare -g MOD_PLAYER_PID=""

last_disk_state="none"  # none, floppy, fallback

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
        start_mod_player "$DEFAULT_MOD_DIR"
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

