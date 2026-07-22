#!/bin/sh
set -eu

INSTALL_DIR="${CONTINUITY_BIN_DIR:-${HOME}/.local/bin}"
TARGET="${INSTALL_DIR}/continuity"

if [ ! -x "$TARGET" ]; then
  printf 'Continuity executable not found at %s\n' "$TARGET" >&2
  exit 2
fi

"$TARGET" codex uninstall "$@"
rm "$TARGET"
printf '%s\n' "Removed the Continuity executable and Codex integration. Vault and config were preserved."
