#!/bin/sh
set -eu

INSTALL_DIR="${GSV_BIN_DIR:-${HOME}/.local/bin}"
TARGET="${INSTALL_DIR}/gsv"

if [ ! -x "$TARGET" ]; then
  printf 'GSV executable not found at %s\n' "$TARGET" >&2
  exit 2
fi

"$TARGET" bridge stop >/dev/null
set +e
"$TARGET" codex uninstall "$@"
cleanup_status=$?
set -e
if [ "$cleanup_status" -ne 0 ]; then
  printf '%s\n' "GSV cleanup is incomplete. The executable was kept so you can run the printed retry command." >&2
  exit "$cleanup_status"
fi
rm "$TARGET"
printf '%s\n' "Removed the GSV executable and verified GSV-owned integration. Vault and config were preserved."
