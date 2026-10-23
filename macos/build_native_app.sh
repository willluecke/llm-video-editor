#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
editor_root=${script_dir:h}
package_root="$script_dir/NativeApp"
build_root="$editor_root/build/macos"
app_bundle="$build_root/LLM Video Editor.app"
contents="$app_bundle/Contents"

cd "$package_root"
/usr/bin/swift build -c release

/bin/mkdir -p "$contents/MacOS" "$contents/Resources"
/bin/cp ".build/release/LLMVideoEditorApp" "$contents/MacOS/LLMVideoEditorApp"
/bin/cp "$package_root/Info.plist" "$contents/Info.plist"
/usr/bin/sed -i '' "s|__EDITOR_ROOT__|$editor_root|g" "$contents/Info.plist"

iconset="$build_root/AppIcon.iconset"
/bin/mkdir -p "$iconset"
for spec in "16 icon_16x16.png" "32 icon_16x16@2x.png" "32 icon_32x32.png" "64 icon_32x32@2x.png" "128 icon_128x128.png" "256 icon_128x128@2x.png" "256 icon_256x256.png" "512 icon_256x256@2x.png" "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
  size=${spec%% *}
  name=${spec#* }
  /usr/bin/sips -z "$size" "$size" "$editor_root/brand/llm-video-editor-icon.png" --out "$iconset/$name" >/dev/null
done
/usr/bin/iconutil -c icns "$iconset" -o "$contents/Resources/AppIcon.icns"
/usr/bin/codesign --force --deep --sign - "$app_bundle" >/dev/null
/bin/echo "$app_bundle"
