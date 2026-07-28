#!/bin/sh
set -eu

INSTALL_DIR="${GSV_BIN_DIR:-${HOME}/.local/bin}"
TARGET="${INSTALL_DIR}/gsv"

if [ -L "$TARGET" ]; then
  printf '%s\n' "This is a managed tool shim. Run 'gsv bridge stop', 'gsv codex uninstall', then 'uv tool uninstall gsv'. The shim was kept." >&2
  exit 2
fi

if [ ! -x "$TARGET" ]; then
  printf 'GSV executable not found at %s\n' "$TARGET" >&2
  exit 2
fi

set +e
"$TARGET" bridge stop >/dev/null
bridge_status=$?
set -e
if [ "$bridge_status" -ne 0 ]; then
  printf '%s\n' "The verified Bridge could not be stopped. The executable was kept." >&2
  exit "$bridge_status"
fi
set +e
cleanup_output=$("$TARGET" --json codex uninstall "$@")
cleanup_status=$?
set -e
printf '%s\n' "$cleanup_output"
cleanup_compact=$(printf '%s' "$cleanup_output" | tr -d '[:space:]')
case "$cleanup_compact" in
  *'"integration_removed":true'*)
    case "$cleanup_compact" in
      *'"recovery_retained":true'*)
        printf '%s\n' "Active GSV integration was removed, but receipt-bound recovery evidence remains. The executable was kept. Inspect and delete only the exact retained_cleanup_paths printed above, then re-run this uninstaller to retire the catalog safely."
        exit 3
        ;;
    esac
    ;;
esac
if [ "$cleanup_status" -ne 0 ]; then
  printf '%s\n' "GSV cleanup is incomplete. The executable was kept so you can run the printed retry command." >&2
  exit "$cleanup_status"
fi
case "$cleanup_compact" in
  '{"ok":true,"result":{"cleanup_complete":true,'*'}'|'{"ok":true,"result":{"cleanup_complete":true}}')
    ;;
  *)
    printf '%s\n' "GSV cleanup output did not verify result.cleanup_complete:true. The executable was kept." >&2
    exit 3
    ;;
esac
rm "$TARGET"
printf '%s\n' "Removed the GSV executable and verified GSV-owned integration. Vault and config were preserved."
