#!/bin/sh
# Release build of the macOS menu bar app: universal (arm64 + x86_64) binary, optional
# signing and notarization. Everyday users do not need this — `limitpilot widget install`
# compiles the app on their own Mac.
#
#   scripts/build_app.sh [OUT_DIR]          # default: dist/
#   CODESIGN_IDENTITY="Developer ID Application: …" scripts/build_app.sh
#   CODESIGN_IDENTITY=… NOTARY_PROFILE=<notarytool keychain profile> scripts/build_app.sh
#
# Without CODESIGN_IDENTITY the app is ad-hoc signed (runs on this Mac only).
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-"$ROOT/dist"}
APP="$OUT/LimitPilot.app"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/limitpilot-release.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

# 1) 번들 뼈대·Info.plist·아이콘·pyz 는 widget build 가 만든다
PYTHONPATH="$ROOT/src" PYTHONDONTWRITEBYTECODE=1 python3 -B -m limitpilot widget build --out "$OUT"

# 2) 두 아키텍처로 다시 컴파일해 하나로 합친다
SRC="$ROOT/src/limitpilot/native/macos/main.swift"
for ARCH in arm64 x86_64; do
  swiftc -O -target "$ARCH-apple-macos11.0" -module-cache-path "$WORK/cache" \
    -framework Cocoa -o "$WORK/LimitPilot-$ARCH" "$SRC"
done
lipo -create -output "$APP/Contents/MacOS/LimitPilot" "$WORK/LimitPilot-arm64" "$WORK/LimitPilot-x86_64"

# 3) 서명(선택) — 신원이 없으면 ad-hoc
if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$APP"
else
  codesign --force --sign - "$APP"
fi
codesign --verify --deep --strict "$APP"

# 4) 공증(선택)
if [ -n "${NOTARY_PROFILE:-}" ] && [ -n "${CODESIGN_IDENTITY:-}" ]; then
  ZIP="$WORK/LimitPilot.zip"
  ditto -c -k --keepParent "$APP" "$ZIP"
  xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
fi
echo "release app: $APP"
