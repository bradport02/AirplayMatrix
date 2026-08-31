#!/bin/bash
# Plain boot-time placeholder screen for the Zero WH kiosk build.
#
# Stands in for the Raspberry Pi firmware splash and Plymouth's graphical
# theme (both disabled -- see config.txt's disable_splash=1 and cmdline.txt's
# plymouth.enable=0/loglevel=1/systemd.show_status=false) with a blank
# console and a short message. Boot on this hardware genuinely takes a
# couple of minutes (see docs/install-pi-zero-wh.md), so a plain "it's
# working" line -- with a cycling "..." so it visibly isn't frozen -- reads
# calmer than either the rainbow splash or raw kernel/systemd boot text.
#
# This never fights the graphical handoff: once lightdm/labwc switch the
# active VT over to the kiosk session, whatever's sitting on tty1 (this
# loop) simply stops being the foreground VT and is never seen again --
# no cleanup step needed here for that reason. The loop just stops itself
# after a generous margin past this hardware's worst-case boot time
# instead of running forever for no benefit.
set -u
exec >/dev/tty1 2>&1
setterm --cursor off 2>/dev/null || true
clear

cols=$(tput cols 2>/dev/null || echo 80)
rows=$(tput lines 2>/dev/null || echo 24)
line1="AirPlay Matrix is booting."
base2="This will take a moment"

row1=$(( rows / 2 - 1 ))
[ "$row1" -lt 0 ] && row1=0
row2=$(( row1 + 1 ))

col1=$(( (cols - ${#line1}) / 2 ))
[ "$col1" -lt 0 ] && col1=0

# Reserve width for the longest ("...") variant so the line never shifts
# horizontally as the dot count cycles -- each redraw left-justifies into
# this fixed field, overwriting the previous frame's trailing dots.
maxlen=$(( ${#base2} + 3 ))
col2=$(( (cols - maxlen) / 2 ))
[ "$col2" -lt 0 ] && col2=0

tput cup "$row1" "$col1" 2>/dev/null
printf '%s' "$line1"

# ~6 minutes at 0.6s/frame -- comfortably past the ~2min boot this build
# actually takes, then just holds the last frame. Harmless either way,
# since the kiosk session will have taken the display over long before
# that regardless of how long this keeps looping.
i=0
max_iters=600
while [ "$i" -lt "$max_iters" ]; do
    dots=$(( i % 4 ))
    frame="$base2$(printf '%*s' "$dots" '' | tr ' ' '.')"
    tput cup "$row2" "$col2" 2>/dev/null
    printf '%-*s' "$maxlen" "$frame"
    i=$((i + 1))
    sleep 0.6
done
