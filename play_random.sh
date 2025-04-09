#!/bin/bash

MOD_DIR=~/mod_player/tiny
#MOD_DIR=~/mod_player/mods

find $MOD_DIR -type f \( -iname "*.mod" -o -iname "*.xm" -o -iname "*.s3m" -o -iname "*.it" \) -print0 | xargs -0 -o -- xmp -R --loop-all
