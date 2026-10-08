#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
CRATE_MANIFEST="$REPO_ROOT/native/trofeo-pump/Cargo.toml"

case "$(uname -s)" in
  Darwin)
    DEFAULT_OUTPUT="$HOME/Library/Application Support/UsageMax Display/trofeo-pump"
    ;;
  Linux)
    DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
    DEFAULT_OUTPUT="$DATA_HOME/usagemax-display/trofeo-pump"
    ;;
  *)
    echo "This script supports native macOS and Linux builds; use the PowerShell script on Windows." >&2
    exit 2
    ;;
esac

OUTPUT_PATH=${1:-$DEFAULT_OUTPUT}
TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/usagemax-display-build.XXXXXX")
trap 'rm -rf "$TEMP_ROOT"' EXIT HUP INT TERM

cargo build --release --manifest-path "$CRATE_MANIFEST" --target-dir "$TEMP_ROOT/target"
mkdir -p "$(dirname -- "$OUTPUT_PATH")"
cp "$TEMP_ROOT/target/release/trofeo-pump" "$OUTPUT_PATH"
chmod 755 "$OUTPUT_PATH"
printf 'Built native transport at: %s\n' "$OUTPUT_PATH"
