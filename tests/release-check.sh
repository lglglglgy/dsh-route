#!/usr/bin/env bash
# 产物自检：拿"刚构建出来的 .run"做一次真装真卸，全程隔离在临时目录，不碰系统服务。
#   ./tests/release-check.sh dist/dsh-route-0.1.0.run
# CI 的发布流程会先跑它，通过了才发 Release。
set -uo pipefail

RUN="${1:-}"
[ -n "$RUN" ] || { echo "用法: $0 dist/dsh-route-<version>.run" >&2; exit 2; }
[ -f "$RUN" ] || { echo "找不到 $RUN" >&2; exit 2; }

pass=0; fail=0
ok()  { printf '  ✓ %s\n' "$*"; pass=$((pass + 1)); }
bad() { printf '  ✗ %s\n' "$*"; fail=$((fail + 1)); }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/dsh-route-rel.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
VER="$(sh "$RUN" --version 2>/dev/null | sed -n 's/.*dsh-route \([0-9.]*\).*/\1/p')"

echo "▸ 产物自检：$RUN${VER:+（版本 ${VER}）}"

sh "$RUN" --version >/dev/null 2>&1 && ok "--version 正常" || bad "--version 失败"
sh "$RUN" --list >/dev/null 2>&1 && ok "--list 正常（载荷可解出）" || bad "--list 失败"

sh "$RUN" --extract="$TMP/x" >/dev/null 2>&1
if [ -f "$TMP/x/dsh-route-$VER/install.sh" ]; then ok "--extract 解出的目录结构正确"; else bad "--extract 内容不对"; fi

# 沙箱安装：不启动服务，unit / state 全部隔离
export DSH_ROUTE_UNIT_DIR="$TMP/units" DSH_ROUTE_STATE_DIR="$TMP/state"
if sh "$RUN" --no-service --prefix "$TMP/prefix" --bin-dir "$TMP/bin" --config-dir "$TMP/config" >"$TMP/install.log" 2>&1; then
  ok "沙箱安装成功（--no-service）"
else
  bad "沙箱安装失败"; tail -5 "$TMP/install.log" | sed 's/^/      /'
fi
missing=""
for f in "$TMP/prefix/daemon.py" "$TMP/prefix/ui.html" "$TMP/prefix/MANIFEST" \
         "$TMP/prefix/assets/nunito-400.woff2" "$TMP/bin/dsh-route" "$TMP/bin/dsh-route-uninstall"; do
  [ -e "$f" ] || missing="$missing $(basename "$f")"
done
[ -z "$missing" ] && ok "装的文件齐全（代码/字体/清单/客户端/卸载器）" || bad "缺少：$missing"
grep -q "^prefix=$TMP/prefix$" "$TMP/prefix/MANIFEST" 2>/dev/null \
  && ok "MANIFEST 记录了本次安装路径" || bad "MANIFEST 内容不对"
grep -q "$TMP/prefix" "$TMP/bin/dsh-route-uninstall" 2>/dev/null \
  && ok "卸载器烧入了本次安装路径" || bad "卸载器没烧入路径"

# dry-run：读自己的清单、且不删任何东西
out="$( "$TMP/bin/dsh-route-uninstall" --dry-run 2>&1 )"
echo "$out" | grep -q "$TMP/prefix/MANIFEST" && ok "dry-run 读的是自己的清单" || bad "dry-run 读错清单"
if [ -e "$TMP/prefix/daemon.py" ] && [ -e "$TMP/bin/dsh-route" ]; then
  ok "dry-run 没改动文件"
else
  bad "dry-run 竟然删了文件"
fi

# 真卸：删代码/客户端/服务定义，保留配置
"$TMP/bin/dsh-route-uninstall" >/dev/null 2>&1
if [ ! -e "$TMP/prefix" ] && [ ! -e "$TMP/bin/dsh-route" ] && [ ! -e "$TMP/bin/dsh-route-uninstall" ]; then
  ok "卸载后代码/客户端/卸载器都没了"
else
  bad "卸载后有残留"
fi
[ -e "$TMP/config/config" ] && ok "配置按设计保留" || bad "配置被误删"

# --purge：连配置与运行态一起清
sh "$RUN" --no-service --prefix "$TMP/prefix" --bin-dir "$TMP/bin" --config-dir "$TMP/config" >/dev/null 2>&1
[ -d "$TMP/state" ] && :  # state 目录可能还没建
"$TMP/bin/dsh-route-uninstall" --purge >/dev/null 2>&1
if [ ! -e "$TMP/prefix" ] && [ ! -e "$TMP/config" ]; then
  ok "--purge 清掉代码与配置"
else
  bad "--purge 没清干净"
fi

echo
echo "  通过 $pass 项，失败 $fail 项"
[ "$fail" = 0 ]
