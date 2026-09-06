#!/bin/bash
# 打包为飞牛 fnOS 可安装的 .fpk。
# 与社区可安装应用（如 fn-fan）保持一致的结构：
#   1) 将 app/ 目录打成 app.tgz
#   2) 再将 app.tgz + cmd + config + ICON*.PNG + manifest + wizard 打成 fpk
# 注意：不要把 app/ 目录直接平铺进 fpk，否则安装器会报「解压app.tgz失败」。
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP=fn-usbcopy
VERSION=$(grep "^version" manifest | awk -F'=' '{print $2}' | tr -d ' ')

chmod +x cmd/* app/*.py tools/*.py 2>/dev/null || true

# 校验关键文件存在
for f in manifest cmd/main app/usbcopy.py app/server.py app/syncengine.py app/usbmonitor.py app/state.py; do
  [ -e "$f" ] || { echo "缺少文件: $f"; exit 1; }
done

# 生成图标（若缺失）
if [ ! -f ICON.PNG ] || [ ! -f ICON_256.PNG ]; then
  python3 tools/gen_icon.py 2>/dev/null || true
fi

rm -f app.tgz "${APP}.fpk" "${APP}-${VERSION}.fpk" 2>/dev/null || true

echo "[1/3] 打包 app 目录 -> app.tgz"
(cd app && tar -zcf ../app.tgz --exclude='__pycache__' --exclude='*.pyc' --exclude='_test*' .)

echo "[2/3] app.tgz MD5:"
md5sum app.tgz 2>/dev/null || true

echo "[3/3] 生成 ${APP}-${VERSION}.fpk"
tar -zcf "${APP}-${VERSION}.fpk" \
  app.tgz cmd config ICON_256.PNG ICON.PNG manifest wizard
rm -f app.tgz 2>/dev/null || true

echo "完成: ${APP}-${VERSION}.fpk"
ls -lh "${APP}-${VERSION}.fpk"
