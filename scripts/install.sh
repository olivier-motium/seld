#!/bin/sh
set -eu

VERSION="${GSV_VERSION:-0.3.0}"
RELEASE_BASE="${GSV_RELEASE_BASE_URL:-https://github.com/olivier-motium/seld/releases/download/v${VERSION}}"
INSTALL_DIR="${GSV_BIN_DIR:-${HOME}/.local/bin}"
TARGET="${INSTALL_DIR}/gsv"

case "$(uname -s)" in
  Darwin) platform="macos" ;;
  Linux) platform="linux" ;;
  *) printf '%s\n' "Unsupported operating system. Use install.ps1 on Windows." >&2; exit 2 ;;
esac

case "$(uname -m)" in
  arm64|aarch64) architecture="arm64" ;;
  x86_64|amd64) architecture="x86_64" ;;
  *) printf '%s\n' "Unsupported CPU architecture: $(uname -m)" >&2; exit 2 ;;
esac

asset="gsv-${platform}-${architecture}"
temporary="$(mktemp -d "${TMPDIR:-/tmp}/gsv-install.XXXXXX")"
staged=""
backup=""
preserve_backup=0
previous_executable_is_compatible() {
  previous="$1"
  set +e
  compatibility_output="$("$TARGET" --json rollback-check "$previous" 2>/dev/null)"
  compatibility_code=$?
  set -e
  [ "$compatibility_code" -eq 0 ] || return 1
  case "$compatibility_output" in
    *'"ok":true'*'"compatible":true'*) return 0 ;;
    *) return 1 ;;
  esac
}
cleanup() {
  rm -rf "$temporary"
  [ -z "$staged" ] || rm -f "$staged"
  if [ "$preserve_backup" -eq 0 ] && [ -n "$backup" ]; then
    rm -f "$backup"
  fi
}
trap cleanup EXIT HUP INT TERM
download="${temporary}/${asset}"

if [ -n "${GSV_BINARY:-}" ]; then
  cp "${GSV_BINARY}" "$download"
  expected="${GSV_BINARY_SHA256:-}"
  if [ -z "$expected" ]; then
    printf '%s\n' "GSV_BINARY_SHA256 is required with GSV_BINARY." >&2
    exit 2
  fi
else
  command -v curl >/dev/null 2>&1 || {
    printf '%s\n' "curl is required to download the release artifact." >&2
    exit 2
  }
  curl --fail --location --proto '=https' --tlsv1.2 --output "$download" "${RELEASE_BASE}/${asset}"
  curl --fail --location --proto '=https' --tlsv1.2 --output "${download}.sha256" "${RELEASE_BASE}/${asset}.sha256"
  expected="$(awk 'NR == 1 { print $1 }' "${download}.sha256")"
fi

if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$download" | awk '{ print $1 }')"
elif command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "$download" | awk '{ print $1 }')"
else
  printf '%s\n' "A SHA-256 tool (sha256sum or shasum) is required." >&2
  exit 2
fi

if [ "$actual" != "$expected" ]; then
  printf '%s\n' "GSV artifact checksum verification failed." >&2
  exit 2
fi

mkdir -p "$INSTALL_DIR"
if [ -L "$TARGET" ]; then
  printf '%s\n' "Refusing to replace a symbolic-link GSV target: $TARGET" >&2
  exit 2
fi
staged="$(mktemp "${INSTALL_DIR}/.gsv.new.XXXXXX")"
install -m 0755 "$download" "$staged"
if ! "$staged" --version >/dev/null; then
  rm -f "$staged"
  printf '%s\n' "The staged GSV executable did not pass its version preflight." >&2
  exit 2
fi
prior_bridge_stopped=0
if [ -e "$TARGET" ]; then
  backup="$(mktemp "${INSTALL_DIR}/.gsv.previous.XXXXXX")"
  cp -p "$TARGET" "$backup"
  set +e
  stop_output="$("$staged" --json bridge stop)"
  stop_status=$?
  set -e
  if [ "$stop_status" -ne 0 ]; then
    rm -f "$backup" "$staged"
    printf '%s\n' "The existing Bridge could not be verified and stopped; the installed executable was left untouched." >&2
    exit "$stop_status"
  fi
  case "$stop_output" in
    *'"ok":true'*'"stopped":true'*) prior_bridge_stopped=1 ;;
    *'"ok":true'*) ;;
    *)
      rm -f "$backup" "$staged"
      printf '%s\n' "The staged GSV executable returned an invalid Bridge stop result." >&2
      exit 2
      ;;
  esac
fi
if ! mv "$staged" "$TARGET"; then
  if [ "$prior_bridge_stopped" -eq 1 ]; then
    "$TARGET" --json bridge open --no-browser >/dev/null 2>&1 || \
      printf '%s\n' "Warning: the previous Bridge could not be restarted after install replacement failed." >&2
  fi
  [ -z "$backup" ] || rm -f "$backup"
  printf '%s\n' "Could not atomically install the GSV executable." >&2
  exit 2
fi

set +e
"$TARGET" setup "$@"
setup_status=$?
set -e
if [ "$setup_status" -eq 4 ]; then
  if [ -n "$backup" ] && [ -e "$backup" ]; then
    preserve_backup=1
  fi
  printf '\nInstalled GSV at %s, but the Bridge needs repair.\n' "$TARGET" >&2
  printf '%s\n' "The candidate and committed Codex integration were kept; no executable rollback was attempted." >&2
  if [ -n "$backup" ] && [ -e "$backup" ]; then
    printf 'The previous executable remains staged at %s.\n' "$backup" >&2
  fi
  printf '%s\n' "Run gsv --json bridge status, then retry gsv bridge open." >&2
  exit "$setup_status"
fi
if [ "$setup_status" -ne 0 ]; then
  set +e
  cleanup_output="$("$TARGET" --json bridge stop 2>/dev/null)"
  cleanup_status=$?
  set -e
  if [ "$cleanup_status" -eq 0 ]; then
    case "$cleanup_output" in
      *'"ok":true'*) ;;
      *) cleanup_status=2 ;;
    esac
  fi
  if [ "$cleanup_status" -ne 0 ]; then
    if [ -n "$backup" ]; then
      preserve_backup=1
      printf '%s\n' "GSV setup failed and its Bridge stop could not be verified (exit $cleanup_status); the previous executable remains staged at $backup." >&2
    else
      printf '%s\n' "GSV setup failed and its Bridge stop could not be verified (exit $cleanup_status); the candidate executable was left in place." >&2
    fi
    exit "$setup_status"
  fi
  if [ -n "$backup" ] && [ -e "$backup" ]; then
    if ! previous_executable_is_compatible "$backup"; then
      preserve_backup=1
      printf '%s\n' "GSV setup failed and the previous executable could not prove it can read the current vault and control ledger; no rollback was performed. The candidate remains installed and the previous executable remains staged at $backup." >&2
      exit "$setup_status"
    fi
    mv "$backup" "$TARGET"
  else
    rm -f "$TARGET"
  fi
  if [ "$prior_bridge_stopped" -eq 1 ]; then
    if ! "$TARGET" --json bridge open --no-browser >/dev/null 2>&1; then
      printf '%s\n' "Warning: the previous executable was restored, but its Bridge could not be restarted." >&2
    fi
  fi
  printf '%s\n' "GSV setup failed; the previous executable was restored and its Bridge restart was attempted." >&2
  exit "$setup_status"
fi
if [ -n "$backup" ] && [ -e "$backup" ]; then
  rm "$backup"
fi

printf '\nInstalled GSV at %s\n' "$TARGET"
case ":${PATH}:" in
  *":${INSTALL_DIR}:"*) ;;
  *) printf 'Add %s to PATH to run gsv directly in future shells.\n' "$INSTALL_DIR" ;;
esac
printf '%s\n' "GSV setup completed. Run gsv to open or verify the Bridge."
printf '%s\n' "Restart Codex, open one fresh task, and run \$gsv-onboard."
