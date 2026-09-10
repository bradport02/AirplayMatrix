#!/bin/bash
# fix-plist-metadata.sh -- restore classic DAAP metadata on the AirPlay 2
# development-branch build.
#
# Why: shairport-sync 5.x (the `development` branch we run for AirPlay 2
# remote control, see upgrade-shairport-dev.sh) switched AirPlay 2 sessions
# to plist-based metadata by default. Its own `ssnc` session events (conn,
# snam, pbeg, pend) still arrive on the metadata pipe, but the classic DAAP
# `core` items (minm/asar/asal/astm) that Software/metadata.py parses for
# title/artist/album/duration no longer do -- so the display sits on the
# "Connection received" screen with a live session and no track.
#
# Upstream ships a documented escape hatch for exactly this, in the
# `diagnostics` block of its own shairport-sync.conf.sample:
#
#   get_plist_metadata = "yes"; // set this temporary setting to "no" to get
#   the older classic metadata information even in AirPlay 2 mode. For
#   backwards compatibility. Deprecated.
#
# Note "temporary" and "Deprecated": upstream intends to remove this, so the
# real long-term fix is to teach Software/metadata.py the plist format. This
# buys back the working display in the meantime.
#
# Safe and idempotent: backs the config up first, refuses to touch a config
# that already sets the key, and restores the backup if shairport-sync fails
# to restart with the new setting.
#
# Run as root (edits /etc, restarts a system service). Directly on the Zero:
#   sudo bash ~/fix-plist-metadata.sh
set -euo pipefail

CONF=/etc/shairport-sync.conf
SERVICE=shairport-sync.service

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root (sudo)" >&2
  exit 1
fi

[[ -f "$CONF" ]] || { echo "$CONF not found" >&2; exit 1; }

if grep -q "get_plist_metadata" "$CONF"; then
  echo "$CONF already sets get_plist_metadata -- nothing to do:"
  grep -n "get_plist_metadata" "$CONF"
  exit 0
fi

BACKUP="${CONF}.bak-$(date +%Y%m%d%H%M%S)"
cp -a "$CONF" "$BACKUP"
echo "backed up $CONF -> $BACKUP"

if grep -qE "^diagnostics *=" "$CONF"; then
  # Existing diagnostics block: insert the key just after its opening brace.
  echo "adding get_plist_metadata to the existing diagnostics block"
  awk '
    /^diagnostics *=/ { inblock = 1 }
    inblock && /\{/ && !done {
      print
      print "  // AirPlay 2 on shairport-sync 5.x defaults to plist metadata, which"
      print "  // drops the classic DAAP core items Software/metadata.py parses."
      print "  get_plist_metadata = \"no\";"
      done = 1
      next
    }
    { print }
  ' "$CONF" > "$CONF.new"
  mv "$CONF.new" "$CONF"
else
  echo "appending a diagnostics block"
  cat >> "$CONF" <<'EOF'

diagnostics = {
  // shairport-sync 5.x sends AirPlay 2 metadata as plists by default, which
  // stops the classic DAAP "core" items (minm/asar/asal/astm) that
  // Software/metadata.py parses for title/artist/album/duration -- leaving
  // the display stuck on "Connection received". Upstream marks this setting
  // temporary/deprecated, so the long-term fix is parsing plist metadata.
  get_plist_metadata = "no";
};
EOF
fi

chmod 644 "$CONF"
echo "restarting $SERVICE"
systemctl restart "$SERVICE"
sleep 2

if ! systemctl is-active --quiet "$SERVICE"; then
  echo "$SERVICE failed to start with the new config -- restoring the backup" >&2
  cp -a "$BACKUP" "$CONF"
  systemctl restart "$SERVICE"
  sleep 1
  systemctl is-active --quiet "$SERVICE" && echo "restored, $SERVICE active again" >&2
  exit 1
fi

echo
echo "done. $SERVICE is active with:"
grep -n "get_plist_metadata" "$CONF"
echo
echo "now play something over AirPlay -- title/artist/album and artwork should appear."
echo "to undo: sudo cp -a $BACKUP $CONF && sudo systemctl restart $SERVICE"
