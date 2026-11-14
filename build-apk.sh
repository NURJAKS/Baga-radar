#!/bin/bash
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET_APK="$HERE/baga-radar-mobile.apk"
BASE_APK="$HERE/_archive/baga-radar.apk"
TMP_DIR="$(mktemp -d)"

echo "Packaging baga-radar-gov.html into mobile APK..."
cp "$BASE_APK" "$TMP_DIR/app.apk"
zip -d "$TMP_DIR/app.apk" "META-INF/*" >/dev/null 2>&1 || true
mkdir -p "$TMP_DIR/assets"
cp "$HERE/baga-radar-gov.html" "$TMP_DIR/assets/index.html"
(cd "$TMP_DIR" && zip -u app.apk assets/index.html >/dev/null)

if [ ! -f ~/.android/debug.keystore ]; then
  keytool -genkey -v -keystore ~/.android/debug.keystore -storepass android -alias androiddebugkey -keypass android -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Android Debug,O=Android,C=US"
fi
jarsigner -sigalg SHA256withRSA -digestalg SHA-256 -keystore ~/.android/debug.keystore -storepass android "$TMP_DIR/app.apk" androiddebugkey >/dev/null
cp "$TMP_DIR/app.apk" "$TARGET_APK"
rm -rf "$TMP_DIR"
echo "Mobile APK successfully built and signed: $TARGET_APK"
