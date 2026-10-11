#!/usr/bin/env bash
# 打发布包：
#   dist/dsh-route-<version>.tar.gz   普通压缩包（解包后 ./install.sh）
#   dist/dsh-route-<version>.run      自解压单文件（结构：shell 桩 + tar.gz 载荷）
#   各附 .sha256
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VER="$(cat VERSION)"
NAME="dsh-route-$VER"
OUT="dist"
BUILT_AT="$(date -u '+%Y-%m-%d %H:%M UTC')"

rm -rf "$OUT" build
mkdir -p "$OUT" "build/$NAME"

# 1) 收集发布树（排除产物、缓存、.git —— 仓库里的 .git 可能很大）
#    注意：GNU tar 与 BSD tar 对 --exclude 的匹配都按成员名（这里是 "./.git/..."），
#    所以 ./x 与 x 两种写法都写上，两个平台才都生效。
tar cf - \
    --exclude=./.git --exclude=.git \
    --exclude=./dist --exclude=dist \
    --exclude=./build --exclude=build \
    --exclude=./.pytest_cache --exclude=__pycache__ --exclude='*.pyc' \
    . | tar xf - -C "build/$NAME"
rm -rf "build/$NAME/dist" "build/$NAME/build"
chmod +x "build/$NAME/install.sh" "build/$NAME/uninstall.sh" "build/$NAME/dist.sh" \
         "build/$NAME/bin/dsh-route" "build/$NAME/bin/dsh-route-uninstall" \
         "build/$NAME/src/daemon.py" "build/$NAME/tests/"*.sh

# 2) 普通 tarball
tar czf "$OUT/$NAME.tar.gz" -C build "$NAME"

# 3) 自解压 .run：桩 + 标记 + 载荷（tar.gz）
PAYLOAD="build/payload.tar.gz"
tar czf "$PAYLOAD" -C build "$NAME"
if command -v sha256sum >/dev/null 2>&1; then
  SUM="$(sha256sum "$PAYLOAD" | awk '{print $1}')"
else
  SUM="$(shasum -a 256 "$PAYLOAD" | awk '{print $1}')"
fi
sed -e "s|@VERSION@|$VER|g" -e "s|@PAYLOAD_SHA256@|$SUM|g" -e "s|@BUILT_AT@|$BUILT_AT|g" \
    selfextract/stub.sh.in > "build/$NAME.run"
cat "$PAYLOAD" >> "build/$NAME.run"
chmod +x "build/$NAME.run"
mv "build/$NAME.run" "$OUT/$NAME.run"

# 4) 校验和
( cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256" \
             && sha256sum "$NAME.run"     > "$NAME.run.sha256" )
rm -rf build

printf '  %s/%s.run        %s  （自解压，单文件）\n' "$OUT" "$NAME" "$(du -h "$OUT/$NAME.run" | cut -f1)"
printf '  %s/%s.tar.gz     %s  （普通压缩包）\n' "$OUT" "$NAME" "$(du -h "$OUT/$NAME.tar.gz" | cut -f1)"
cat "$OUT/$NAME.run.sha256"
