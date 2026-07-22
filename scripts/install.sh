#!/bin/sh
set -eu

VERSION="${GSV_VERSION:-0.1.0}"
RELEASE_BASE="${GSV_RELEASE_BASE_URL:-https://github.com/olivier-motium/gsv/releases/download/v${VERSION}}"
INSTALL_DIR="${GSV_BIN_DIR:-${HOME}/.local/bin}"
TARGET="${INSTALL_DIR}/gsv"

if ! command -v codex >/dev/null 2>&1; then
  printf '%s\n' "Codex CLI was not found on PATH. Install Codex Desktop or the Codex CLI first." >&2
  exit 2
fi

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
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
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
backup=""
if [ -e "$TARGET" ]; then
  backup="$(mktemp "${INSTALL_DIR}/.gsv.previous.XXXXXX")"
  cp -p "$TARGET" "$backup"
fi
if ! mv "$staged" "$TARGET"; then
  [ -z "$backup" ] || rm -f "$backup"
  printf '%s\n' "Could not atomically install the GSV executable." >&2
  exit 2
fi

set +e
"$TARGET" setup "$@"
setup_status=$?
set -e
if [ "$setup_status" -ne 0 ]; then
  if [ -n "$backup" ] && [ -e "$backup" ]; then
    mv "$backup" "$TARGET"
  else
    rm -f "$TARGET"
  fi
  printf '%s\n' "GSV setup failed; the previous executable was restored." >&2
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
printf '%s\n' "Restart Codex, open a fresh task, and ask: What do you remember?"
