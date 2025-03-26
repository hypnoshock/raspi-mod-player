#!/bin/bash

# ~/mod_player/tiny
# ~/mod_player/mods

find ~/mod_player/mods -type f \( -iname "*.mod" -o -iname "*.xm" -o -iname "*.s3m" -o -iname "*.it" \) -print0 | xargs -0 -o -- xmp -R --loop-all
