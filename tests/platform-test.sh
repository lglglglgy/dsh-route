#!/usr/bin/env bash
# 平台分支验证（Linux/WSL 上可跑，用桩 ps 验证 macOS 逻辑；macOS 上直接测真机 ps）
set -uo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"      # 从脚本位置推导，不写死路径
pass=0; fail=0
ok(){ printf '  ✓ %s\n' "$*"; pass=$((pass+1)); }
bad(){ printf '  ✗ %s\n' "$*"; fail=$((fail+1)); }
# macOS 没有 setsid：用子 shell + nohup 脱离
detach_start(){ if command -v setsid >/dev/null 2>&1; then setsid nohup "$@" >/dev/null 2>&1 & else ( nohup "$@" >/dev/null 2>&1 & ); fi; }
# BSD sed 的 -i 需要后缀参数，所以不用 -i
edit_in_place(){ f="$1"; shift; sed "$@" "$f" > "$f.tmp.$$" && mv "$f.tmp.$$" "$f"; }

echo "▸ 1) macOS 发现路径（DSH_ROUTE_PLATFORM=macos + 桩 ps）"
cat > /tmp/fake-ps <<'EOF'
#!/bin/sh
# 模拟 macOS: ps -Ao comm=,args=
# 注意同时覆盖两种形态：args 含 argv[0] / 不含 argv[0]
cat <<'OUT'
/usr/sbin/sshd
/usr/bin/ssh ssh h100 -o RemoteCommand=none sleep 300
/usr/bin/ssh hwdocker
ssh-agent
grep ssh
/usr/bin/ssh ssh -N -L 3080:127.0.0.1:3080 h100
/usr/libexec/sshd-keygen-wrapper
OUT
EOF
chmod +x /tmp/fake-ps
out=$(cd "$D/src" && DSH_ROUTE_PLATFORM=macos DSH_ROUTE_PS=/tmp/fake-ps python3 -c '
import daemon
cfg = {"EXTRA_EXCLUDE": "", "POWERSHELL": "", "DSH_HOME": ""}
targets, reliable = daemon.discover(cfg)
print("targets=%s reliable=%s platform=%s" % (sorted(t[0] for t in targets), reliable, daemon.PLATFORM))
')
echo "    $out"
echo "$out" | grep -q "targets=\['h100', 'hwdocker'\]" && ok "从 ps 里正确识别 h100 / hwdocker" || bad "识别结果不对"
echo "$out" | grep -q "reliable=True" && ok "本机发现视为可信（不会被误判为不可靠）" || bad "reliable 不对"
echo "$out" | grep -q "platform=macos" && ok "平台识别为 macos" || bad "平台识别不对"

echo "▸ 2) launchd plist 渲染 + XML 校验"
rm -rf /tmp/pb
DSH_ROUTE_UNIT_DIR=/tmp/pb/units DSH_ROUTE_STATE_DIR=/tmp/pb/state \
  bash "$D/install.sh" --service-kind launchd --prefix /tmp/pb/prefix \
     --bin-dir /tmp/pb/bin --config-dir /tmp/pb/config --no-service >/tmp/pb.log 2>&1 \
  && ok "launchd 模式安装完成（未调用 launchctl）" || { bad "安装失败"; tail -5 /tmp/pb.log | sed 's/^/      /'; }
P=/tmp/pb/units/com.dshroute.agent.plist
if [ -f "$P" ]; then
  python3 - "$P" <<'PY' && ok "plist 是合法 XML 且关键字段/替换正确" || bad "plist 结构或替换有问题"
import plistlib, sys
d = plistlib.load(open(sys.argv[1], "rb"))
assert d["Label"] == "com.dshroute.agent", d.get("Label")
assert d["RunAtLoad"] is True and d["KeepAlive"] is True
assert d["ProgramArguments"][1].endswith("/prefix/daemon.py"), d["ProgramArguments"]
assert d["EnvironmentVariables"]["DSH_ROUTE_PREFIX"] == "/tmp/pb/prefix"
assert d["StandardOutPath"].endswith("Library/Logs/dsh-route.log")
assert "@" not in "".join(map(str, d["ProgramArguments"]))
print("      Label=%s  Program=%s" % (d["Label"], " ".join(d["ProgramArguments"])))
PY
else
  bad "没有生成 plist"
fi

echo "▸ 3) systemd 系统服务模板渲染"
sed -e "s|@PREFIX@|/opt/dsh-route|g" -e "s|@CONFIGDIR@|/etc/dsh-route|g" \
    -e "s|@PYTHON@|/usr/bin/python3|g" -e "s|@USER@|tester|g" \
    -e "s|@STATEDIR@|/var/lib/dsh-route|g" \
    "$D/service/dsh-route.system.service.in" > /tmp/sys.service
grep -q "^User=tester" /tmp/sys.service && ok "系统 unit 带 User=" || bad "缺 User="
grep -q "RuntimeDirectory=dsh-route" /tmp/sys.service && ok "系统 unit 用 RuntimeDirectory 提供运行目录" || bad "缺 RuntimeDirectory"
grep -q "DSH_ROUTE_STATE=/var/lib/dsh-route" /tmp/sys.service && ok "系统 unit 带上运行态目录" || bad "缺 DSH_ROUTE_STATE"
! grep -q "@[A-Z]*@" /tmp/sys.service && ok "占位符已全部替换" || bad "仍有未替换占位符"

echo "▸ 4) 无服务管理器（none）兜底：装 + 真跑 + 停"
rm -rf /tmp/nb
DSH_ROUTE_UNIT_DIR=/tmp/nb DSH_ROUTE_STATE_DIR=/tmp/nb/state \
  bash "$D/install.sh" --service-kind none --prefix /tmp/nb/prefix \
     --bin-dir /tmp/nb/bin --config-dir /tmp/nb/config --no-service >/tmp/nb.log 2>&1 \
  && ok "none 模式安装完成" || { bad "安装失败"; tail -5 /tmp/nb.log | sed 's/^/      /'; }
# 换成不冲突的端口，手动启动
edit_in_place /tmp/nb/config/config 's/^BASE_PORT=.*/BASE_PORT=3200/; s/^CONTROL_PORT=.*/CONTROL_PORT=3191/'
if [ -x /tmp/nb/prefix/run.sh ]; then
  detach_start /tmp/nb/prefix/run.sh
  sleep 4
  code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:3191/api/status || true)
  [ "$code" = "200" ] && ok "兜底进程起来了（3191 控制页 200）" || bad "兜底进程没起来（HTTP ${code}）"
  DSH_ROUTE_PREFIX=/tmp/nb/prefix DSH_ROUTE_CONFIG=/tmp/nb/config/config bin=/tmp/nb/bin/dsh-route
  DSH_ROUTE_PREFIX=/tmp/nb/prefix DSH_ROUTE_CONFIG=/tmp/nb/config/config \
    DSH_ROUTE_STATE=/tmp/nb/state /tmp/nb/bin/dsh-route where 2>/dev/null | head -3 | sed 's/^/      /'
  pkill -f "/tmp/nb/prefix/daemon.py" 2>/dev/null || true
  sleep 1
  code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' http://127.0.0.1:3191/api/status || true)
  [ "$code" != "200" ] && ok "已停掉（端口不再响应）" || bad "进程没停干净"
else
  bad "没有生成 run.sh"
fi

echo "▸ 5) 清单驱动卸载（卸载器自带本次安装路径，不依赖发行包）"
rm -rf /tmp/mf5
DSH_ROUTE_UNIT_DIR=/tmp/mf5/units DSH_ROUTE_STATE_DIR=/tmp/mf5/state \
  bash "$D/install.sh" --service-kind none --prefix /tmp/mf5/prefix \
     --bin-dir /tmp/mf5/bin --config-dir /tmp/mf5/config --no-service >/dev/null 2>&1
[ -f /tmp/mf5/prefix/MANIFEST ] && ok "安装时写了清单 MANIFEST" || bad "没有清单"
grep -q "/tmp/mf5/prefix" /tmp/mf5/bin/dsh-route-uninstall \
  && ok "卸载器烧入了本次安装的路径" || bad "卸载器没烧入路径（会找错清单）"
out=$(/tmp/mf5/bin/dsh-route-uninstall 2>&1)
echo "$out" | grep -q "/tmp/mf5/prefix/MANIFEST" && ok "卸载时读的是自己的清单" || bad "读错清单"
echo "$out" | grep -q "干净" && ok "自己的文件清理干净" || bad "有残留"
[ -d /tmp/mf5/config ] && ok "配置按设计保留" || bad "配置被误删"
[ ! -e /tmp/mf5/bin/dsh-route-uninstall ] && ok "卸载器连同自己一起删掉（PATH 不留残留）" || bad "卸载器没删掉"

rm -rf /tmp/mf6
DSH_ROUTE_UNIT_DIR=/tmp/mf6/units DSH_ROUTE_STATE_DIR=/tmp/mf6/state \
  bash "$D/install.sh" --service-kind none --prefix /tmp/mf6/prefix \
     --bin-dir /tmp/mf6/bin --config-dir /tmp/mf6/config --no-service >/dev/null 2>&1
mkdir -p /tmp/mf6/state && : > /tmp/mf6/state/balance-history.json
/tmp/mf6/bin/dsh-route-uninstall --purge >/dev/null 2>&1
if [ ! -e /tmp/mf6/prefix ] && [ ! -e /tmp/mf6/config ] && [ ! -e /tmp/mf6/state ]; then
  ok "--purge 连配置与历史一起清"
else
  bad "--purge 没清干净"
fi
rm -rf /tmp/mf5 /tmp/mf6

echo
echo "  通过 $pass 项，失败 $fail 项"
[ "$fail" = 0 ]
