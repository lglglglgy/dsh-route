#!/usr/bin/env bash
# 冒烟测试：服务状态 / 控制页 / 字体 / 路径穿越 / API 结构 / 每个转发端口
#   ./tests/smoke.sh             用当前生效配置
#   CTL=3091 ./tests/smoke.sh    指定控制页端口
set -uo pipefail

CONFIG="${DSH_ROUTE_CONFIG:-${DSH_ROUTE_CONFIG_DIR:-$HOME/.config/dsh-route}/config}"
CTL="${CTL:-}"
if [ -z "$CTL" ] && [ -f "$CONFIG" ]; then
  CTL=$(awk -F= '/^[[:space:]]*CONTROL_PORT/{sub(/#.*/,"",$2); gsub(/[^0-9]/,"",$2); print $2; exit}' "$CONFIG")
fi
CTL="${CTL:-3090}"
API="http://127.0.0.1:$CTL"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok()  { printf '  ✓ %s\n' "$*"; pass=$((pass+1)); }
bad() { printf '  ✗ %s\n' "$*"; fail=$((fail+1)); }

DATA="${DSH_ROUTE_PREFIX:-$HOME/.local/share/dsh-route}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/dsh-route"
KIND="$(cat "$DATA/.service-kind" 2>/dev/null || true)"
if [ -z "$KIND" ]; then
  case "$(uname -s)" in Darwin) KIND=launchd ;; *) KIND=systemd ;; esac
fi

svc_active() {
  case "$KIND" in
    systemd)        systemctl --user is-active --quiet dsh-route 2>/dev/null ;;
    systemd-system) systemctl is-active --quiet dsh-route 2>/dev/null ;;
    launchd)        launchctl print "gui/$(id -u)/com.dshroute.agent" >/dev/null 2>&1 ;;
    none)           [ -f "$STATE_DIR/dsh-route.pid" ] && kill -0 "$(cat "$STATE_DIR/dsh-route.pid")" 2>/dev/null ;;
    *)              return 1 ;;
  esac
}

echo "▸ 冒烟测试（控制页 ${API}，平台 $(uname -s)，服务 ${KIND}）"

if svc_active; then ok "服务运行中（${KIND}）"; else bad "服务未运行（${KIND}）"; fi
case "$KIND" in
  systemd)        systemctl --user is-enabled --quiet dsh-route 2>/dev/null && ok "开机自启 enabled" || bad "未设置自启" ;;
  systemd-system) systemctl is-enabled --quiet dsh-route 2>/dev/null && ok "开机自启 enabled" || bad "未设置自启" ;;
  launchd)        [ -f "$HOME/Library/LaunchAgents/com.dshroute.agent.plist" ] && ok "launchd plist 已安装" || bad "缺少 launchd plist" ;;
  none)
    if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q "$DATA/run.sh"; then
      ok "crontab @reboot 兜底自启已配置"
    else
      warn_msg="没有 crontab 兜底自启（可手动把 $DATA/run.sh 加到登录项）"
      printf '  ! %s\n' "$warn_msg"
    fi ;;
esac

code=$(curl -s -m 5 -o "$TMP/page.html" -w '%{http_code}' "$API/")
if [ "$code" = 200 ] && grep -q "DSH WEB ROUTE" "$TMP/page.html"; then
  ok "控制页 200 且内容正确"
else
  bad "控制页异常（HTTP ${code}）"
fi

code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$API/assets/nunito-400.woff2")
if [ "$code" = 200 ]; then ok "字体资源 200"; else bad "字体资源 HTTP $code"; fi

code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$API/assets/../daemon.py")
if [ "$code" = 404 ]; then ok "路径穿越被拒（404）"; else bad "路径穿越未拦截（HTTP ${code}）"; fi

curl -s -m 5 "$API/api/status" > "$TMP/status.json"
if python3 - "$TMP/status.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert "rows" in d, "缺少 rows"
assert "selected" not in d, "顶层仍有旧的 selected 字段"
for r in d["rows"]:
    assert "selected" not in r, "行内仍有 selected 字段"
print("  目标 %d 台，候选远端端口 %s" % (len(d["rows"]), ",".join(map(str, d.get("candidate_ports") or []))))
PY
then ok "API 结构正常（无主口字段）"; else bad "API 结构异常"; fi

if python3 - "$TMP/status.json" <<'PY'
import json, sys, os, subprocess, tempfile
d = json.load(open(sys.argv[1]))
jar = tempfile.mktemp()
n = 0
good_all = True
for r in d["rows"]:
    for f in r.get("forwards") or []:
        if not f.get("forwarded"):
            continue
        n += 1
        url = "http://127.0.0.1:%d/" % f["local_port"]
        if f.get("token"):
            url += "?token=" + f["token"]
        out = subprocess.run(["curl", "-s", "-L", "-m", "8", "-c", jar, "-b", jar,
                              "-o", "/tmp/smoke-fwd.html", "-w", "%{http_code}", url],
                             capture_output=True, text=True)
        code = out.stdout.strip()
        body = open("/tmp/smoke-fwd.html", encoding="utf-8", errors="replace").read()
        is_dsh = "dsh" in body.lower()
        # 200 = 带 token 进去了；401 + dsh 特征响应 = 隧道通、只是这台没取到 token（正常）
        good = (code == "200" and is_dsh) or (code == "401" and "dsh web authentication required" in body)
        note = "带 token 打开成功" if code == "200" else ("隧道正常（无 token，401 信任栅栏）" if good else "")
        print("  %s %-18s 远端 %d → 本地 %d  HTTP %s  %s"
              % ("✓" if good else "✗", r["host"], f["remote_port"], f["local_port"], code, note))
        good_all &= good
os.path.exists(jar) and os.unlink(jar)
if n == 0:
    print("  ! 当前没有在线目标（先 ssh 到目标机再测转发）")
raise SystemExit(0 if good_all else 1)
PY
then ok "所有转发端口可用"; else bad "存在不可用的转发端口"; fi

echo
echo "  通过 $pass 项，失败 $fail 项"
[ "$fail" = 0 ]
