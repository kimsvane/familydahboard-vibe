#!/bin/bash
# Bygger FamilyBridge.app (universal) og FamilyBridge-<version>.pkg.
# Kræver kun Xcode Command Line Tools (swiftc, lipo, codesign, pkgbuild).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VERSION="${1:-1.0.0}"
BUNDLE_ID="dk.ssvanefamily.bridge"
NAME="FamilyBridge"
BUILD_DIR="build"
APP="$BUILD_DIR/$NAME.app"
STAGE="$BUILD_DIR/stage"

echo "==> Rydder $BUILD_DIR"
rm -rf "$BUILD_DIR"
TEMPLATE_DIR="$STAGE/Library/Application Support/FamilyBridge"
mkdir -p "$APP/Contents/MacOS" "$STAGE/Applications" "$TEMPLATE_DIR"

# Monterey (macOS 12) er det ældste understøttede system. Sæt MACOS_MIN
# hvis bridgeen skal køre på Big Sur (11) eller ældre.
MACOS_MIN="${MACOS_MIN:-12.0}"

echo "==> Kompilerer universal binary ($NAME $VERSION, macOS >= $MACOS_MIN)"
for ARCH in arm64 x86_64; do
  swiftc -swift-version 5 -target "${ARCH}-apple-macosx${MACOS_MIN}" -O \
    -o "$BUILD_DIR/$NAME-$ARCH" Sources/FamilyBridge/*.swift
done
lipo -create "$BUILD_DIR/$NAME-arm64" "$BUILD_DIR/$NAME-x86_64" -output "$APP/Contents/MacOS/$NAME"
rm -f "$BUILD_DIR/$NAME-arm64" "$BUILD_DIR/$NAME-x86_64"

echo "==> Samler .app"
# Versionen skrives ind i Info.plist, så /health kan rapportere den rigtige
# version i stedet for en fastsat konstant.
sed -e "s/__VERSION__/$VERSION/g" Resources/Info.plist > "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist" >/dev/null
printf 'APPL????' > "$APP/Contents/PkgInfo"
# Uden dette lægger macOS ._AppleDouble-filer ind i pkg-payload.
export COPYFILE_DISABLE=1

echo "==> Ad-hoc kodesigner"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "==> Samler staging-rod + LaunchAgent-skabelon"
# Skabelonen lægges i Application Support, IKKE i /Library/LaunchDaemons:
# EventKit skal køre i den loggede brugeres LaunchAgent-session, aldrig som LaunchDaemon.
sed -e "s/__BUNDLE_ID__/$BUNDLE_ID/g" -e "s/__LABEL__/$BUNDLE_ID/g" \
    -e "s#__EXECUTABLE__#/Applications/$NAME.app/Contents/MacOS/$NAME#g" \
    Resources/LaunchAgent.plist.template > "$TEMPLATE_DIR/LaunchAgent.plist.template"
cp -R "$APP" "$STAGE/Applications/"

# Fjern xattrs/AppleDouble, så pkg-payload ikke fyldes med ._-filer.
xattr -cr "$STAGE" 2>/dev/null || true

echo "==> Bygger komponent-pkg"
pkgbuild --root "$STAGE" \
  --identifier "$BUNDLE_ID" \
  --version "$VERSION" \
  --install-location / \
  --scripts "$HERE/scripts" \
  "$BUILD_DIR/$NAME.pkg" >/dev/null

OUT="$HERE/$NAME-$VERSION.pkg"
cp "$BUILD_DIR/$NAME.pkg" "$OUT"

echo "==> Verificerer"
# Bevidst ingen CharacterSet.whitespaces: det symbol findes ikke i Montneys
# Swift-overlay og får dyld til at nægte at starte appen.
BINARY="$APP/Contents/MacOS/$NAME"
if nm -u "$BINARY" 2>/dev/null | grep -q "CharacterSetV11whitespaces"; then
  echo "FEJL: buildet bruger CharacterSet.whitespaces, som ikke virker på macOS 12."
  exit 1
fi
echo "  arkitekturer: $(lipo -archs "$BINARY")"
echo "  min. macOS:   $(otool -l "$BINARY" | awk '/LC_BUILD_VERSION/{f=1} f&&/minos/{print $2; exit}')"
codesign --verify --deep --strict "$APP"
installer -pkginfo -pkg "$OUT" >/dev/null
xar -tf "$OUT"

echo
echo "Færdig:"
echo "  $OUT  ($(du -h "$OUT" | cut -f1))"
echo "  $APP  (applikationen alene)"
echo
echo "Installér på Mac Mini'en med:"
echo "  sudo installer -pkg $OUT -target /"
