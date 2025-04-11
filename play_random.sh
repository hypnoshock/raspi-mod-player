#!/bin/bash

MOD_DIR=~/mod_player/tiny
#MOD_DIR=~/mod_player/mods

FLOPPY_DEV="/dev/sda"
FLOPPY_MOUNT="$HOME/floppy"

# Check if floppy exists
if [ -b "$FLOPPY_DEV" ]; then
    mkdir -p "$FLOPPY_MOUNT"

    if ! mountpoint -q "$FLOPPY_MOUNT"; then
        echo "Mounting floppy..."
        sudo /bin/mount "$FLOPPY_DEV" "$FLOPPY_MOUNT" && MOD_DIR="$FLOPPY_MOUNT"
    else
        echo "Floppy already mounted"
        MOD_DIR="$FLOPPY_MOUNT"
    fi
fi

# Play mod files!
find $MOD_DIR -type f \( -iname "*.mod" -o -iname "*.xm" -o -iname "*.s3m" -o -iname "*.it" \) -print0 | xargs -0 -o -- xmp -R --loop-all
