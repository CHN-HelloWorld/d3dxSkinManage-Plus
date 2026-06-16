#!/usr/bin/env bash
# 获取使用 MIT 许可的 Microsoft Fluent 图标子集。
# 图标按下方清单中的尺寸及样式保存到 resources/icons/fluent。
# 使用 bash 执行本脚本，已有有效文件会被跳过。
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASE="https://raw.githubusercontent.com/microsoft/fluentui-system-icons/main/assets"
DEST="$ROOT/resources/icons/fluent"

# 制表符分隔字段：分类名称、上游资源目录、资源标识、尺寸、样式。
CATALOG=$(cat <<'EOF'
navigation/overview	Apps List	apps_list	24	regular
navigation/gallery	Image Multiple	image_multiple	24	regular
navigation/store	Building Shop	building_shop	24	regular
navigation/settings	Settings	settings	24	regular
navigation/tools	Toolbox	toolbox	24	regular
actions/launch	Rocket	rocket	24	regular
actions/game	Games	games	24	regular
actions/menu	Navigation	navigation	24	regular
actions/import	Arrow Import	arrow_import	24	regular
actions/export	Arrow Export	arrow_export	24	regular
actions/refresh	Arrow Sync	arrow_sync	24	regular
actions/refresh-circle	Arrow Clockwise	arrow_clockwise	24	regular
actions/search	Search	search	24	regular
actions/folder	Folder	folder	24	regular
actions/folder-open	Folder Open	folder_open	24	regular
actions/new-folder	Folder Add	folder_add	24	regular
actions/edit	Edit	edit	24	regular
actions/dismiss	Dismiss	dismiss	24	regular
actions/remove	Delete	delete	24	regular
actions/add	Add	add	24	regular
actions/mod	Box	box	24	regular
actions/image	Image	image	24	regular
actions/copy	Copy	copy	24	regular
actions/open	Open	open	24	regular
actions/back	Arrow Left	arrow_left	24	regular
actions/up	Arrow Up	arrow_up	24	regular
actions/down	Arrow Download	arrow_download	24	regular
actions/list-view	Apps List Detail	apps_list_detail	24	regular
actions/card-view	Grid	grid	24	regular
actions/more	More Horizontal	more_horizontal	24	regular
actions/fullscreen	Full Screen Maximize	full_screen_maximize	24	regular
actions/fullscreen-exit	Full Screen Minimize	full_screen_minimize	24	regular
actions/chevron-up	Chevron Up	chevron_up	24	regular
actions/chevron-down	Chevron Down	chevron_down	24	regular
actions/chevron-right	Chevron Right	chevron_right	24	regular
status/enabled	Checkmark Circle	checkmark_circle	24	regular
status/disabled	Prohibited	prohibited	24	regular
status/warning	Warning	warning	24	regular
status/hidden	Eye Off	eye_off	24	regular
status/visible	Eye	eye	24	regular
status/installed	Checkmark	checkmark	24	regular
status/update	Arrow Sync	arrow_sync	24	regular
status/info	Info	info	24	regular
status/error	Error Circle	error_circle	24	regular
meta/views	Eye	eye	24	regular
meta/likes	Heart	heart	24	regular
meta/downloads	Arrow Download	arrow_download	24	regular
meta/date	Calendar	calendar	24	regular
meta/character	Person	person	24	regular
meta/category	Folder	folder	24	regular
meta/star	Star	star	24	filled
meta/star-outline	Star	star	24	regular
EOF
)

ok=0; fail=0; skip=0
FAILED=""
while IFS=$'\t' read -r relname folder snake size style; do
  [ -z "$relname" ] && continue
  out="$DEST/$relname.svg"
  if [ -s "$out" ] && head -c 5 "$out" | grep -q "<svg"; then
    skip=$((skip+1)); continue
  fi
  mkdir -p "$(dirname "$out")"
  efolder="${folder// /%20}"
  url="$BASE/$efolder/SVG/ic_fluent_${snake}_${size}_${style}.svg"
  if curl -sSL --max-time 30 "$url" -o "$out" 2>/dev/null && [ -s "$out" ] && head -c 5 "$out" | grep -q "<svg"; then
    ok=$((ok+1)); printf 'OK   %-26s <- %s\n' "$relname" "ic_fluent_${snake}_${size}_${style}.svg"
  else
    fail=$((fail+1)); FAILED="$FAILED\n  $relname  ($url)"; rm -f "$out"
    printf 'FAIL %-26s <- %s\n' "$relname" "$url"
  fi
done <<< "$CATALOG"

echo ""
echo "downloaded=$ok skipped=$skip failed=$fail"
[ -n "$FAILED" ] && printf 'FAILED:%b\n' "$FAILED"
exit "$((fail > 0))"
