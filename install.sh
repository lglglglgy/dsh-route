#!/usr/bin/env bash
# dsh-route 安装 / 升级脚本（幂等，可重复执行；Linux · WSL · macOS）
#
#   ./install.sh                       安装（或升级）并启动服务
#   ./install.sh --dry-run             只打印将要做什么
#   ./install.sh --no-service          只放文件、装服务定义，不启动
#   ./install.sh --service-kind K      指定服务类型：auto|systemd|systemd-system|launchd|none
#   ./install.sh --system              装成 systemd 系统服务（需要 sudo）
#   ./install.sh --prefix DIR          代码目录（默认 ~/.local/share/dsh-route）
#   ./install.sh --bin-dir DIR         客户端目录（默认 ~/bin）
#   ./install.sh --config-dir DIR      配置目录（默认 ~/.config/dsh-route）
#   ./install.sh --host-alias NAME     访问别名，默认 dsh.local（幂等写进 /etc/hosts，卸载时移除）
#   ./install.sh --no-host-alias       不碰 /etc/hosts
#   ./install.sh --uninstall            卸载（保留配置与历史）
#   ./install.sh --uninstall --purge    连配置、余额历史一起删除
#   ./install.sh --dry-run --uninstall  只打印会删什么
#
# 卸载器会一并装到 $BIN_DIR/dsh-route-uninstall，之后直接 `dsh-route uninstall` 即可，
# 不依赖发行包是否还留着；它读安装时写的 $PREFIX/MANIFEST 精确删除。
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$SELF_DIR/VERSION" 2>/dev/null || echo 0.0.0)"

PREFIX="${DSH_ROUTE_PREFIX:-$HOME/.local/share/dsh-route}"
BIN_DIR="${DSH_ROUTE_BIN_DIR:-$HOME/bin}"
CONFIG_DIR="${DSH_ROUTE_CONFIG_DIR:-$HOME/.config/dsh-route}"
UNIT_DIR_DEFAULT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LOGDIR="$HOME/Library/Logs"
STATE_DIR="${DSH_ROUTE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/dsh-route}"
HOST_ALIAS="${DSH_ROUTE_HOST_ALIAS:-dsh.local}"     # 访问别名；置空表示不改 hosts
HOSTS_FILE="${DSH_ROUTE_HOSTS_FILE:-/etc/hosts}"    # 测试可覆盖
HOSTS_MARK="# dsh-route"
WIN_PS="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
WIN_HOSTS="/mnt/c/Windows/System32/drivers/etc/hosts"
WIN_PS1="/mnt/c/Windows/Temp/dsh-route-hosts.ps1"
WIN_HOSTS_REQ=""                                    # 空=交互式时自动问；1=强制；0=跳过
SVC=dsh-route
LABEL=com.dshroute.agent
SYSTEM_UNIT_DIR=/etc/systemd/system

DRY=0; UNINSTALL=0; PURGE=0; NO_SERVICE=0; CUSTOM_UNIT=0
SERVICE_KIND="${DSH_ROUTE_SERVICE_KIND:-auto}"
# 服务定义目录：仅供测试/内部使用（环境变量），不作为命令行参数暴露给用户
if [ -n "${DSH_ROUTE_UNIT_DIR:-}" ]; then UNIT_DIR="$DSH_ROUTE_UNIT_DIR"; CUSTOM_UNIT=1; fi

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)       PREFIX="$2"; shift 2 ;;
    --bin-dir)      BIN_DIR="$2"; shift 2 ;;
    --config-dir)   CONFIG_DIR="$2"; shift 2 ;;
    --host-alias)   HOST_ALIAS="$2"; shift 2 ;;
    --no-host-alias) HOST_ALIAS=""; shift ;;
    --windows-hosts)    WIN_HOSTS_REQ=1; shift ;;
    --no-windows-hosts) WIN_HOSTS_REQ=0; shift ;;
    --service-kind) SERVICE_KIND="$2"; shift 2 ;;
    --system)       SERVICE_KIND=systemd-system; shift ;;
    --no-service)   NO_SERVICE=1; shift ;;
    --dry-run)      DRY=1; shift ;;
    --uninstall)    UNINSTALL=1; shift ;;
    --purge)        PURGE=1; shift ;;
    -h|--help)
      sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
  esac
done

say()  { printf '  %s\n' "$*"; }
step() { printf '\n▸ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '\n✗ %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then say "[dry-run] $*"; else "$@"; fi; }
iso_now() { date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u; }
# 后台脱离启动：macOS 没有 setsid，用子 shell + nohup 让进程被 init 收养
detach_start() {
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$@" >/dev/null 2>&1 &
  else
    ( nohup "$@" >/dev/null 2>&1 & )
  fi
}
# 就地改文件：BSD sed 的 -i 需要后缀参数，干脆不用 -i
replace_in_file() {  # file sed-expr...
  f="$1"; shift
  sed "$@" "$f" > "$f.tmp.$$" && mv "$f.tmp.$$" "$f"
}

# ── 平台 / 服务类型 ─────────────────────────────────────────
OS="$(uname -s)"
is_wsl() { [ -r /proc/version ] && grep -qi microsoft /proc/version 2>/dev/null; }

if [ "$SERVICE_KIND" = auto ]; then
  if [ "$OS" = Darwin ]; then
    SERVICE_KIND=launchd
  elif systemctl --user show-environment >/dev/null 2>&1; then
    SERVICE_KIND=systemd
  else
    SERVICE_KIND=none
  fi
fi

case "$SERVICE_KIND" in
  systemd)         UNIT_DIR="${UNIT_DIR:-$UNIT_DIR_DEFAULT}" ;;
  systemd-system)  UNIT_DIR="$SYSTEM_UNIT_DIR" ;;
  launchd)         UNIT_DIR="${UNIT_DIR:-$LAUNCHD_DIR}" ;;
  none)            UNIT_DIR="${UNIT_DIR:-$PREFIX}" ;;
  *) die "不支持的服务类型：${SERVICE_KIND}（可选 auto|systemd|systemd-system|launchd|none）" ;;
esac
[ "$SERVICE_KIND" != systemd ] && [ "$CUSTOM_UNIT" = 1 ] && true

service_desc() {
  case "$SERVICE_KIND" in
    systemd)        echo "systemd --user（$UNIT_DIR/$SVC.service）" ;;
    systemd-system) echo "systemd 系统服务（$UNIT_DIR/$SVC.service）" ;;
    launchd)        echo "launchd 用户代理（$UNIT_DIR/$LABEL.plist）" ;;
    none)           echo "无服务管理器：后台进程 + 自启兜底（$PREFIX/run.sh）" ;;
  esac
}

# ── 环境检查 ────────────────────────────────────────────────
check_env() {
  step "环境检查"
  say "系统：$OS$([ "$SERVICE_KIND" = systemd ] && is_wsl && echo "（WSL）")"
  command -v python3 >/dev/null || die "缺少 python3"
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)'; then
    say "python3 $(python3 -c 'import platform;print(platform.python_version())')"
  else
    die "需要 python3 ≥ 3.9"
  fi
  # 会话解析：主力是"远端解压"（远端跑着 dsh，node/zstd/python 至少有一样），
  # 本机这三样有任意一个就能兜底。不要求 python 3.14。
  if python3 -c 'from compression import zstd' 2>/dev/null; then
    say "会话解析：本机 compression.zstd（python ≥3.14）"
  elif python3 -c 'import zstandard' 2>/dev/null; then
    say "会话解析：本机 zstandard 模块"
  elif command -v zstd >/dev/null 2>&1; then
    say "会话解析：本机 zstd 命令"
  else
    say "会话解析：由远端完成（远端有 node/zstd/python 即可）；本机没装也无妨"
  fi
  command -v curl >/dev/null || die "缺少 curl"
  command -v ssh  >/dev/null || die "缺少 ssh"
  say "curl / ssh 就绪"
  say "服务托管：$(service_desc)"
  if [ "$SERVICE_KIND" = systemd ]; then
    if grep -qiE '^\s*systemd\s*=\s*true' /etc/wsl.conf 2>/dev/null; then
      say "WSL 已启用 systemd"
    elif is_wsl; then
      warn "建议在 /etc/wsl.conf 加 [boot] systemd=true（否则 systemctl --user 不可用）"
    fi
    if command -v loginctl >/dev/null 2>&1; then
      linger=$(loginctl show-user "${USER:-$(id -un)}" -p Linger --value 2>/dev/null || echo unknown)
      [ "$linger" = yes ] || warn "未开启 linger：注销后服务会停。建议执行 loginctl enable-linger ${USER:-$(id -un)}"
    fi
  fi
  if [ "$SERVICE_KIND" = systemd-system ]; then
    command -v sudo >/dev/null 2>&1 || die "systemd 系统服务需要 sudo"
    say "将用 sudo 写入 ${SYSTEM_UNIT_DIR}（每次装/升都要一次授权）"
  fi
  if [ "$SERVICE_KIND" = none ]; then
    warn "没有 systemd/launchd：会以后台进程运行，并用 crontab @reboot 兜底自启（若可用）"
  fi
  if [ "$SERVICE_KIND" = systemd ] && is_wsl; then
    if ls /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe >/dev/null 2>&1 \
       || command -v powershell.exe >/dev/null 2>&1; then
      say "能调用 Windows PowerShell（宿主机 ssh 目标可被发现）"
    else
      warn "找不到 Windows PowerShell：宿主机侧 ssh 目标发现不到（WSL 内的 ssh 仍可发现）"
    fi
  fi
}

# ── 卸载 ────────────────────────────────────────────────────
# 服务定义是否属于本次要卸载的 prefix（沙箱卸载时不要动到线上服务）
unit_is_ours() { [ -f "$1" ] && grep -qF "$PREFIX" "$1" 2>/dev/null; }

do_uninstall() {
  step "卸载 dsh-route"

  # 1) 停：只停"属于本 prefix"的服务定义；最后再按进程路径清残留
  if unit_is_ours "$UNIT_DIR/$SVC.service" || unit_is_ours "$UNIT_DIR_DEFAULT/$SVC.service"; then
    if [ "$CUSTOM_UNIT" = 0 ]; then systemctl --user disable --now "$SVC" >/dev/null 2>&1 || true; fi
  fi
  if unit_is_ours "$SYSTEM_UNIT_DIR/$SVC.service" && command -v sudo >/dev/null 2>&1; then
    sudo systemctl disable --now "$SVC" >/dev/null 2>&1 || true
  fi
  if command -v launchctl >/dev/null 2>&1 && [ "$CUSTOM_UNIT" = 0 ] && unit_is_ours "$UNIT_DIR/$LABEL.plist"; then
    # 注意：launchd 的 Label 是共享的，指定了 --unit-dir（沙箱）时绝不能 bootout，否则会停掉线上 agent
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || \
      launchctl unload "$UNIT_DIR/$LABEL.plist" >/dev/null 2>&1 || true
  fi
  if [ -f "$STATE_DIR/dsh-route.pid" ]; then
    p=$(cat "$STATE_DIR/dsh-route.pid" 2>/dev/null || true)
    if [ -n "${p:-}" ] && ps -p "$p" -o command= 2>/dev/null | grep -qF "$PREFIX"; then
      kill "$p" 2>/dev/null || true
    fi
  fi
  pkill -f "$PREFIX/daemon.py" 2>/dev/null || true
  sleep 1

  # 2) 服务定义（指定了 --unit-dir 就不碰真实默认目录，避免误删线上服务）
  run rm -f "$UNIT_DIR/$SVC.service" "$UNIT_DIR/$LABEL.plist"
  if [ "$CUSTOM_UNIT" = 0 ]; then
    if [ "$UNIT_DIR" != "$UNIT_DIR_DEFAULT" ]; then run rm -f "$UNIT_DIR_DEFAULT/$SVC.service"; fi
    if [ "$UNIT_DIR" != "$LAUNCHD_DIR" ]; then run rm -f "$LAUNCHD_DIR/$LABEL.plist"; fi
  fi
  if [ -f "$SYSTEM_UNIT_DIR/$SVC.service" ] && command -v sudo >/dev/null 2>&1; then
    run sudo rm -f "$SYSTEM_UNIT_DIR/$SVC.service"
    run sudo systemctl daemon-reload || true
  fi
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  if command -v launchctl >/dev/null 2>&1 && launchctl list 2>/dev/null | grep -q "$LABEL"; then
    if [ "$CUSTOM_UNIT" = 0 ]; then run launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true; fi
  fi

  # 3) crontab 兜底自启
  if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q "dsh-route"; then
    if [ "$DRY" = 1 ]; then
      say "[dry-run] 清理 crontab 里的 dsh-route 行"
    else
      crontab -l 2>/dev/null | grep -v "dsh-route" | crontab - 2>/dev/null || true
      say "已清理 crontab 自启"
    fi
  fi

  # 4) 代码 / 客户端 / 运行日志与运行目录
  run rm -rf "$PREFIX"
  run rm -f "$BIN_DIR/dsh-route"
  run rm -rf "${XDG_RUNTIME_DIR:-/tmp}/dsh-route" "/tmp/dsh-route"
  run rm -f "$HOME/Library/Logs/dsh-route.log"
  # 安装过程中可能残留的临时文件（异常中断时）
  run rm -f /tmp/dsh-route.service.* /tmp/dsh-route.sys.* /tmp/com.dshroute.agent.plist.* \
            /tmp/dsh-route.run.* /tmp/dsh-route-manifest.* 2>/dev/null || true

  # 5) 配置与历史：默认保留，--purge 才删
  if [ "$PURGE" = 1 ]; then
    run rm -rf "$CONFIG_DIR" "$STATE_DIR"
    say "已按 --purge 删除配置与历史"
  fi

  # 6) 残留自检
  step "残留检查"
  left=0
  if [ "$DRY" = 0 ]; then
    res_paths="$PREFIX $BIN_DIR/dsh-route ${XDG_RUNTIME_DIR:-/tmp}/dsh-route /tmp/dsh-route $HOME/Library/Logs/dsh-route.log"
    if [ "$CUSTOM_UNIT" = 0 ]; then
      res_paths="$res_paths $UNIT_DIR_DEFAULT/$SVC.service $LAUNCHD_DIR/$LABEL.plist"
    fi
    for p in $res_paths; do
      if [ -e "$p" ]; then say "! 残留：仍在 $p"; left=$((left + 1)); fi
    done
    if command -v pgrep >/dev/null 2>&1 && pgrep -f "$PREFIX/daemon.py" >/dev/null 2>&1; then
      say "! 残留：仍有进程 $(pgrep -f "$PREFIX/daemon.py" | tr '\n' ' ')"
      left=$((left + 1))
    fi
    # launchd / crontab 是共享的（Label、crontab 都属于用户级），沙箱卸载时不该把它们算成自己的残留
    if [ "$CUSTOM_UNIT" = 0 ]; then
      if command -v launchctl >/dev/null 2>&1 && launchctl list 2>/dev/null | grep -q "$LABEL"; then
        say "! 残留：launchd 里仍注册着 $LABEL"
        left=$((left + 1))
      fi
      if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q "dsh-route"; then
        say "! 残留：crontab 里仍有 dsh-route 行"
        left=$((left + 1))
      fi
    fi
  fi
  if [ "$left" = 0 ]; then
    say "干净：代码 / 客户端 / 服务定义 / 运行日志 已全部移除"
  else
    warn "有 $left 处残留（见上）"
  fi
  if [ "$PURGE" != 1 ]; then
    say "按设计保留（要一起删加 --purge）：$CONFIG_DIR 、 $STATE_DIR"
  fi
  exit 0
}
if [ "$UNINSTALL" = 1 ]; then
  # 优先用"安装时放进 PATH 的卸载器"（有清单，精确）；老版本没装过才走兜底逻辑
  U="$BIN_DIR/dsh-route-uninstall"
  if [ -x "$U" ]; then
    extra=""
    [ "$PURGE" = 1 ] && extra="--purge"
    [ "$DRY" = 1 ] && extra="$extra --dry-run"
    DSH_ROUTE_PREFIX="$PREFIX" DSH_ROUTE_MANIFEST="$PREFIX/MANIFEST" exec "$U" $extra
  fi
  do_uninstall   # 兜底：没有卸载器的旧安装
fi

check_env

# ── 代码与客户端 ────────────────────────────────────────────
step "安装代码到 ${PREFIX}（版本 ${VERSION}）"
run mkdir -p "$PREFIX/assets" "$CONFIG_DIR" "$BIN_DIR"
run install -m 755 "$SELF_DIR/src/daemon.py" "$PREFIX/daemon.py"
run install -m 644 "$SELF_DIR/src/ui.html"   "$PREFIX/ui.html"
for f in "$SELF_DIR"/src/assets/*; do
  [ -f "$f" ] || continue
  run install -m 644 "$f" "$PREFIX/assets/$(basename "$f")"
done
run install -m 644 "$SELF_DIR/VERSION" "$PREFIX/VERSION"
run install -m 644 "$SELF_DIR/README.md" "$PREFIX/README.md"
run install -m 755 "$SELF_DIR/bin/dsh-route" "$BIN_DIR/dsh-route"
# 卸载器：把本次安装的实际路径替换进去，让每个安装自带"知道自己在哪"的卸载器
if [ "$DRY" = 0 ]; then
  sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@BIN_DIR@|$BIN_DIR|g" \
      -e "s|@CONFIGDIR@|$CONFIG_DIR|g" -e "s|@STATEDIR@|$STATE_DIR|g" \
      "$SELF_DIR/bin/dsh-route-uninstall" > "$BIN_DIR/dsh-route-uninstall"
  chmod 755 "$BIN_DIR/dsh-route-uninstall"
else
  say "[dry-run] 安装卸载器 → $BIN_DIR/dsh-route-uninstall"
fi
say "daemon.py / ui.html / assets(字体) / VERSION / README → $PREFIX"
say "客户端 → $BIN_DIR/dsh-route，卸载器 → $BIN_DIR/dsh-route-uninstall（之后可 dsh-route uninstall）"

# ── 配置：绝不覆盖已有文件 ──────────────────────────────────
step "配置 $CONFIG_DIR/config"
if [ -f "$CONFIG_DIR/config" ]; then
  say "已存在，保持不动"
  missing=$(comm -23 \
    <(grep -oE '^[A-Z_]+=' "$SELF_DIR/config/config.example" | tr -d '=' | sort -u) \
    <(grep -oE '^[A-Z_]+=' "$CONFIG_DIR/config" | tr -d '=' | sort -u) || true)
  [ -n "$missing" ] && warn "新版还支持这些键（可自行加）：$(echo $missing | tr ' ' ',')"
else
  run install -m 644 "$SELF_DIR/config/config.example" "$CONFIG_DIR/config"
  say "已按模板生成"
fi
run install -m 644 "$SELF_DIR/config/config.example" "$CONFIG_DIR/config.example"

# ── 服务定义 ────────────────────────────────────────────────
PY="$(command -v python3)"
install_service() {
  step "安装服务定义（${SERVICE_KIND}）"
  case "$SERVICE_KIND" in
    systemd)
      run mkdir -p "$UNIT_DIR"
      sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@CONFIGDIR@|$CONFIG_DIR|g" -e "s|@PYTHON@|$PY|g" -e "s|@STATEDIR@|$STATE_DIR|g" \
          "$SELF_DIR/service/dsh-route.service.in" > /tmp/dsh-route.service.$$
      if [ -f "$UNIT_DIR/$SVC.service" ] && cmp -s /tmp/dsh-route.service.$$ "$UNIT_DIR/$SVC.service"; then
        say "unit 无变化"
      else
        run install -m 644 /tmp/dsh-route.service.$$ "$UNIT_DIR/$SVC.service"
        say "unit 已写入 $UNIT_DIR/$SVC.service"
        run systemctl --user daemon-reload || true
      fi
      rm -f /tmp/dsh-route.service.$$ ;;
    systemd-system)
      sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@CONFIGDIR@|$CONFIG_DIR|g" -e "s|@PYTHON@|$PY|g" -e "s|@STATEDIR@|$STATE_DIR|g" \
          -e "s|@USER@|$(id -un)|g" \
          "$SELF_DIR/service/dsh-route.system.service.in" > /tmp/dsh-route.sys.$$
      run sudo install -m 644 /tmp/dsh-route.sys.$$ "$UNIT_DIR/$SVC.service"
      say "系统 unit 已写入 $UNIT_DIR/$SVC.service"
      run sudo systemctl daemon-reload || true
      rm -f /tmp/dsh-route.sys.$$ ;;
    launchd)
      run mkdir -p "$UNIT_DIR" "$LOGDIR"
      sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@CONFIGDIR@|$CONFIG_DIR|g" -e "s|@PYTHON@|$PY|g" -e "s|@STATEDIR@|$STATE_DIR|g" \
          -e "s|@LOGDIR@|$LOGDIR|g" -e "s|@HOME@|$HOME|g" \
          "$SELF_DIR/service/com.dshroute.agent.plist.in" > /tmp/$LABEL.plist.$$
      if [ -f "$UNIT_DIR/$LABEL.plist" ] && cmp -s /tmp/$LABEL.plist.$$ "$UNIT_DIR/$LABEL.plist"; then
        say "plist 无变化"
      else
        run install -m 644 /tmp/$LABEL.plist.$$ "$UNIT_DIR/$LABEL.plist"
        say "plist 已写入 $UNIT_DIR/$LABEL.plist"
      fi
      rm -f /tmp/$LABEL.plist.$$ ;;
    none)
      run mkdir -p "$STATE_DIR"
      cat > /tmp/dsh-route.run.$$ <<EOF
#!/bin/sh
# dsh-route 后台启动脚本（无 systemd/launchd 时的兜底）
export DSH_ROUTE_PREFIX="$PREFIX"
export DSH_ROUTE_CONFIG="$CONFIG_DIR/config"
export DSH_ROUTE_KEY_FILE="$CONFIG_DIR/deepseek.key"
export DSH_ROUTE_STATE="$STATE_DIR"
mkdir -p "\${XDG_RUNTIME_DIR:-/tmp}/dsh-route" "$STATE_DIR"
exec "$PY" "$PREFIX/daemon.py" >> "$STATE_DIR/daemon.log" 2>&1
EOF
      run install -m 755 /tmp/dsh-route.run.$$ "$PREFIX/run.sh"
      rm -f /tmp/dsh-route.run.$$
      say "启动脚本 $PREFIX/run.sh（日志 $STATE_DIR/daemon.log）" ;;
  esac
}
install_service
if [ "$DRY" = 0 ]; then printf '%s\n' "$SERVICE_KIND" > "$PREFIX/.service-kind"; fi

# ── 访问别名：写 hosts，让地址变成 http://dsh.local:3090/ ──
HOSTS_APPLIED=0
HOSTS_WIN_APPLIED=0

# WSL：Windows 的 hosts 才是"浏览器真正用的那份"（WSL 的每次启动会重建）
win_ps1_write() {   # add|remove
  if [ "$1" = remove ]; then
    cat > "$WIN_PS1" <<'PSEOF'
param([string]$Path = (Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'))
$kept = Get-Content -LiteralPath $Path | Where-Object { $_ -notmatch '# dsh-route' }
Set-Content -LiteralPath $Path -Value $kept -Encoding ASCII
Write-Host "已移除 dsh-route 相关行 -> $Path"
PSEOF
  else
    cat > "$WIN_PS1" <<PSEOF
param([string]\$Path = (Join-Path \$env:SystemRoot 'System32\\drivers\\etc\\hosts'))
if ((Get-Content -LiteralPath \$Path) -match '${HOST_ALIAS}') {
  Write-Host "已存在，跳过 -> \$Path"
} else {
  Add-Content -LiteralPath \$Path -Value "127.0.0.1\`t${HOST_ALIAS}\`t# dsh-route"
  Write-Host "已写入 127.0.0.1 ${HOST_ALIAS} -> \$Path"
}
PSEOF
  fi
}

# 触发 UAC（管理员 PowerShell）执行刚生成的脚本
win_ps1_run_elevated() {
  "$WIN_PS" -NoProfile -Command \
    "Start-Process -Verb RunAs -Wait powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\\Windows\\Temp\\dsh-route-hosts.ps1'"
}

apply_host_alias_windows() {
  [ "$WIN_HOSTS_REQ" = 0 ] && return 0
  [ "$DRY" = 0 ] || return 0
  [ -x "$WIN_PS" ] && [ -d /mnt/c/Windows/Temp ] || return 0      # 不是 WSL 就算了
  if grep -q "$HOST_ALIAS" "$WIN_HOSTS" 2>/dev/null; then
    say "Windows hosts 里已有 ${HOST_ALIAS}（永久有效，不需要再弹 UAC）"
    HOSTS_WIN_APPLIED=1
    return 0
  fi
  if [ "$WIN_HOSTS_REQ" = 1 ] || { [ -t 0 ] && [ -z "${DSH_ROUTE_NO_PROMPT:-}" ]; }; then
    win_ps1_write add
    say "接下来会在 Windows 弹一次 UAC 授权（把 $HOST_ALIAS 写进 Windows 的 hosts）…"
    if win_ps1_run_elevated; then
      if grep -q "$HOST_ALIAS" "$WIN_HOSTS" 2>/dev/null; then
        HOSTS_WIN_APPLIED=1
        say "Windows hosts 已写入：${HOST_ALIAS}（卸载时会移除）"
      else
        warn "UAC 已确认但没读到写入结果，可稍后跑 dsh-route host-alias --windows"
      fi
    else
      warn "没能弹 UAC，可稍后跑：dsh-route host-alias --windows"
    fi
  else
    say "想让 Windows 浏览器也用 http://$HOST_ALIAS:${CTL:-3090}/ ：跑 dsh-route host-alias --windows（弹一次 UAC）"
  fi
}
apply_host_alias() {
  if [ -z "$HOST_ALIAS" ]; then
    say "按 --no-host-alias 跳过 /etc/hosts"
    return 0
  fi
  if [ "$DRY" = 1 ]; then
    say "[dry-run] 在 $HOSTS_FILE 写入「127.0.0.1 ${HOST_ALIAS}」（带 $HOSTS_MARK 标记，卸载时移除）"
    return 0
  fi
  if [ ! -f "$HOSTS_FILE" ]; then
    warn "找不到 ${HOSTS_FILE}，跳过别名"
    return 0
  fi
  tmp="$(mktemp)"
  grep -v "$HOSTS_MARK" "$HOSTS_FILE" > "$tmp" 2>/dev/null || cp "$HOSTS_FILE" "$tmp" 2>/dev/null || : > "$tmp"
  printf '127.0.0.1\t%s\t%s\n::1\t%s\t%s\n' "$HOST_ALIAS" "$HOSTS_MARK" "$HOST_ALIAS" "$HOSTS_MARK" >> "$tmp"

  alias_manual_hint() {
    say "没改 ${HOSTS_FILE}（需要管理员权限）。想用 http://$HOST_ALIAS:${CTL:-3090}/ ："
    say "    dsh-route host-alias            # 会提示输入密码"
    say "    echo '127.0.0.1 $HOST_ALIAS' | sudo tee -a $HOSTS_FILE"
    if is_wsl; then
      say "  WSL 提示：/etc/hosts 每次启动会重建，建议写到 Windows 那份（浏览器在 Windows 上也是这份）："
      say "    dsh-route host-alias --windows   # 会弹一次 UAC"
    fi
  }

  if [ -w "$HOSTS_FILE" ]; then
    cat "$tmp" > "$HOSTS_FILE"
  elif sudo -n true 2>/dev/null; then
    sudo cp "$tmp" "$HOSTS_FILE"
  elif [ -t 0 ] && [ -z "${DSH_ROUTE_NO_PROMPT:-}" ] && command -v sudo >/dev/null 2>&1; then
    # 交互式安装：直接要一次密码写进去（非交互场景不会走到这里，免得卡住）
    say "写 ${HOSTS_FILE} 需要管理员权限，下面会提示输入密码（不想改就 Ctrl-C 后加 --no-host-alias）"
    if sudo cp "$tmp" "$HOSTS_FILE"; then
      rm -f "$tmp"
      HOSTS_APPLIED=1
      say "访问别名已写入 ${HOSTS_FILE}：${HOST_ALIAS}（带 $HOSTS_MARK 标记，卸载时移除）"
      return 0
    fi
    warn "sudo 没成功，跳过别名"
    rm -f "$tmp"
    alias_manual_hint
    return 0
  else
    rm -f "$tmp"
    alias_manual_hint
    return 0
  fi
  rm -f "$tmp"
  HOSTS_APPLIED=1
  say "访问别名已写入 ${HOSTS_FILE}：${HOST_ALIAS}（带 $HOSTS_MARK 标记，卸载时移除）"
}
step "访问别名（/etc/hosts）"
apply_host_alias
apply_host_alias_windows

# ── 安装清单：卸载器靠它精确删除（用户不需要记任何路径）────────
case "$SERVICE_KIND" in
  systemd|systemd-system) UNIT_PATH="$UNIT_DIR/$SVC.service" ;;
  launchd)                UNIT_PATH="$UNIT_DIR/$LABEL.plist" ;;
  *)                      UNIT_PATH="" ;;
esac
LOG_PATH=""; [ "$SERVICE_KIND" = launchd ] && LOG_PATH="$LOGDIR/dsh-route.log"
CRON_LINE=""; [ "$SERVICE_KIND" = none ] && CRON_LINE="@reboot $PREFIX/run.sh"
if [ "$DRY" = 0 ]; then
  {
    printf 'version=%s\n'      "$VERSION"
    printf 'installed_at=%s\n' "$(iso_now)"
    printf 'os=%s\n'           "$OS"
    printf 'service_kind=%s\n' "$SERVICE_KIND"
    printf 'prefix=%s\n'       "$PREFIX"
    printf 'bin_dir=%s\n'      "$BIN_DIR"
    printf 'client=%s\n'       "$BIN_DIR/dsh-route"
    printf 'uninstaller=%s\n'  "$BIN_DIR/dsh-route-uninstall"
    printf 'config_dir=%s\n'   "$CONFIG_DIR"
    printf 'state_dir=%s\n'    "$STATE_DIR"
    printf 'unit_name=%s\n'    "$SVC"
    printf 'unit_file=%s\n'    "$UNIT_PATH"
    printf 'label=%s\n'        "$LABEL"
    printf 'runtime_dir=%s\n'  "${XDG_RUNTIME_DIR:-/tmp}/dsh-route"
    printf 'log_file=%s\n'     "$LOG_PATH"
    printf 'crontab_line=%s\n' "$CRON_LINE"
    printf 'host_alias=%s\n'   "$HOST_ALIAS"
    printf 'hosts_file=%s\n'   "$HOSTS_FILE"
    printf 'hosts_applied=%s\n' "$HOSTS_APPLIED"
    printf 'hosts_windows=%s\n' "$HOSTS_WIN_APPLIED"
    printf 'files=daemon.py ui.html assets/ VERSION README.md\n'
  } > "$PREFIX/MANIFEST"
fi
say "安装清单：$PREFIX/MANIFEST（卸载器读它，不需要记路径）"

# ── 启动 ────────────────────────────────────────────────────
wait_ready() {
  CTL=$(awk -F= '/^[[:space:]]*CONTROL_PORT/{sub(/#.*/,"",$2); gsub(/[^0-9]/,"",$2); print $2; exit}' \
        "$CONFIG_DIR/config" 2>/dev/null || true); CTL="${CTL:-3090}"
  i=0
  while [ $i -lt 20 ]; do
    code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$CTL/api/status" || true)
    [ "$code" = "200" ] && return 0
    i=$((i+1)); sleep 1
  done
  return 1
}

if [ "$NO_SERVICE" = 0 ] && [ "$DRY" = 0 ]; then
  step "启动服务（${SERVICE_KIND}）"
  case "$SERVICE_KIND" in
    systemd)        systemctl --user enable --now "$SVC" || die "启动失败：systemctl --user status $SVC" ;;
    systemd-system) sudo systemctl enable --now "$SVC"   || die "启动失败：systemctl status $SVC" ;;
    launchd)
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      if ! launchctl bootstrap "gui/$(id -u)" "$UNIT_DIR/$LABEL.plist" 2>/dev/null; then
        launchctl load -w "$UNIT_DIR/$LABEL.plist" 2>/dev/null || true
      fi
      launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true ;;
    none)
      detach_start "$PREFIX/run.sh"
      sleep 1
      pid=$(pgrep -f "$PREFIX/daemon.py" 2>/dev/null | head -1 || true)
      if [ -n "${pid:-}" ]; then
        printf '%s\n' "$pid" > "$STATE_DIR/dsh-route.pid"
        say "已启动，pid $pid"
      fi
      if command -v crontab >/dev/null 2>&1; then
        ( crontab -l 2>/dev/null | grep -v "$PREFIX/run.sh" || true
          echo "@reboot $PREFIX/run.sh" ) | crontab - 2>/dev/null \
          && say "已写入 crontab @reboot 兜底自启" || warn "crontab 写入失败，请自行把 $PREFIX/run.sh 加到自启"
      else
        warn "没有 crontab：请自行把 $PREFIX/run.sh 加到登录自启（如 shell profile）"
      fi ;;
  esac
  if wait_ready; then
    n=$(curl -s -m 3 "http://127.0.0.1:$CTL/api/status" \
        | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("rows") or []))' 2>/dev/null || echo '?')
    say "服务就绪：http://127.0.0.1:$CTL/   当前在线目标：$n 台"
  else
    warn "控制页没起来，最近日志："
    case "$SERVICE_KIND" in
      systemd)        systemctl --user status "$SVC" --no-pager -n 8 2>/dev/null | sed 's/^/     /' || true ;;
      systemd-system) sudo systemctl status "$SVC" --no-pager -n 8 2>/dev/null | sed 's/^/     /' || true ;;
      launchd)        tail -n 8 "$LOGDIR/dsh-route.log" 2>/dev/null | sed 's/^/     /' || true ;;
      none)           tail -n 8 "$STATE_DIR/daemon.log" 2>/dev/null | sed 's/^/     /' || true ;;
    esac
    die "安装未完成"
  fi
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR 不在 PATH 里，命令行客户端请用 $BIN_DIR/dsh-route 或把它加进 PATH" ;;
esac

step "完成"
say "服务托管：$(service_desc)"
say "命令行：dsh-route / dsh-route url <host> / dsh-route key"
if [ -n "$HOST_ALIAS" ] && [ "$HOSTS_APPLIED" = 1 ]; then
  say "控制页：http://$HOST_ALIAS:${CTL:-3090}/    （也支持 http://localhost:${CTL:-3090}/）"
else
  say "控制页：http://localhost:${CTL:-3090}/    （或 http://127.0.0.1:${CTL:-3090}/）"
  [ -n "$HOST_ALIAS" ] && say "  想用 http://$HOST_ALIAS:${CTL:-3090}/：dsh-route host-alias"
fi
say "注意：点进某台机器的 dsh web 时用 127.0.0.1（远端信任栅栏按 Host 判定）"
say "升级：再跑一次本脚本即可（配置与余额历史不会被动）"
