#!/bin/sh
# macOS / Linux 用の起動スクリプト。ダブルクリック（またはターミナルで実行）。
# 設定は同じフォルダの server_config.txt を編集してください。
cd "$(dirname "$0")" || exit 1

# 使える python のうち、3.9 以上で **いちばん新しいもの** を選ぶ。
# ⚠️版の名前を並べた順に選ぶ方式にすると、新しい版が名簿に無いときに
#   古いほうが選ばれる（実機で python3.14 があるのに 3.11 が選ばれた）。
# ⚠️名前を手で並べると、そこに無い版（3.16 など）を拾えない。**その場で作る**。
NAMES="python3 python"
i=9
while [ "$i" -le 40 ]; do
  NAMES="$NAMES python3.$i"
  i=$((i + 1))
done

PY=""
PYV=0
for c in $NAMES; do
  command -v "$c" >/dev/null 2>&1 || continue
  v=$("$c" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null) || continue
  [ "$v" -ge 309 ] 2>/dev/null || continue
  if [ "$v" -gt "$PYV" ]; then PYV="$v"; PY="$c"; fi
done

if [ -z "$PY" ]; then
  echo "Python 3.9 以降が見つかりませんでした。"
  echo "https://www.python.org/downloads/ から入れてから、もう一度実行してください。"
  echo ""
  echo "Enter キーで閉じます。"
  read -r _
  exit 1
fi

echo "起動します（設定: server_config.txt / $($PY --version 2>&1)）"
echo ""
"$PY" serve.py
echo ""
echo "サーバーを終了しました。Enter キーで閉じます。"
read -r _
