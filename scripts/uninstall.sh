#!/bin/sh
set -eu

INSTALL_DIR="${GSV_BIN_DIR:-${HOME}/.local/bin}"
TARGET="${INSTALL_DIR}/gsv"

if [ ! -x "$TARGET" ]; then
  printf 'GSV executable not found at %s\n' "$TARGET" >&2
  exit 2
fi

"$TARGET" codex uninstall "$@"
rm "$TARGET"
printf '%s\n' "Removed the GSV executable and Codex integration. Vault and config were preserved."
