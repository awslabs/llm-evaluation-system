#!/usr/bin/env bash
# Fetch + cache the official AWS Architecture Icons package (idempotent).
# Icons are published by AWS at https://aws.amazon.com/architecture/icons/
# and are licensed for use in architecture diagrams / documentation.
#
# Usage: fetch_icons.sh [CACHE_DIR]
# Default CACHE_DIR: <skill>/cache
set -euo pipefail

CACHE_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/cache}"
ICONS_DIR="$CACHE_DIR/aws-icons"
mkdir -p "$CACHE_DIR"

if [ -d "$ICONS_DIR/Architecture-Service-Icons_"* ] 2>/dev/null || \
   ls -d "$ICONS_DIR"/Architecture-Service-Icons_* >/dev/null 2>&1; then
  echo "icons already cached: $ICONS_DIR"
  exit 0
fi

# The package URL is versioned by release date + content hash. AWS updates it
# quarterly; if this 404s, fetch https://aws.amazon.com/architecture/icons/ and
# grab the current "Icon-package_*.zip" link from the page.
PKG_URL="https://d1.awsstatic.com/onedam/marketing-channels/website/aws/en_US/architecture/approved/architecture-icons/Icon-package_04302026.4705b90f5aa45b019271a2699e9ce9b97b941ee1.zip"

echo "downloading AWS icon package..."
curl -fsSL -o "$CACHE_DIR/aws-icons.zip" "$PKG_URL" || {
  echo "ERROR: download failed. Get the current 'Icon-package_*.zip' URL from"
  echo "https://aws.amazon.com/architecture/icons/ and set PKG_URL in this script."
  exit 1
}
mkdir -p "$ICONS_DIR"
( cd "$ICONS_DIR" && unzip -q ../aws-icons.zip && rm -rf __MACOSX )
echo "icons cached: $ICONS_DIR"
