#!/usr/bin/env python3
"""dsh-route 守护进程

- 自动发现"当前连着的远端"（宿主机 Windows 侧 ssh.exe + WSL 侧 ssh 进程）
- 对每台机器的候选端口（默认 3080/3081/3082）逐个**验证**：只有返回
  `401 + "dsh web authentication required"` 的才算 dsh web，验证通过才建立转发
- 每个"主机:远端端口"一条隧道，主口优先占用 BASE_PORT
- 采集余额、status.deepseek.com 状态/事件、远端最近会话(tokens/运行/阻塞)、用量
- 页面右上角设置里可录入 DeepSeek API key
"""
from __future__ import annotations

import base64
import glob
import html as html_mod
import io
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from compression import zstd as _zstd          # python ≥ 3.14
except Exception:
    _zstd = None
try:
    import zstandard as _zstandard                  # pip install zstandard（3.8+ 都能用）
except Exception:
    _zstandard = None


_ZSTD_NODE_JS = (
    'const fs=require("fs"),z=require("zlib");'
    'const b=fs.readFileSync(%s);'
    'const M=Buffer.from([0x28,0xb5,0x2f,0xfd]);'          # zstd 帧魔数
    'let s=[],i=0;while((i=b.indexOf(M,i))!==-1){s.push(i);i+=4}s.push(b.length);'
    'let o=[],p=null;'
    'for(let k=0;k<s.length-1;k++){'
    'const c=p?Buffer.concat([p,b.subarray(s[k],s[k+1])]):b.subarray(s[k],s[k+1]);'
    'try{o.push(z.zstdDecompressSync(c));p=null}catch(e){p=c}}'   # 解不开就和下一帧合并再试
    'process.stdout.write(Buffer.concat(o))')


def zstd_backend() -> str:
    """本机可用的 zstd 解压后端（用于状态展示；远端解压是主力）。"""
    if _zstd is not None:
        return "python compression.zstd"
    if _zstandard is not None:
        return "python zstandard"
    for name in ("zstd", "zstdcat", "unzstd"):
        if shutil.which(name):
            return "%s 命令" % name
    if shutil.which("node"):
        return "node（逐帧）"
    return "无（靠远端解压）"


def local_zstd_decompress(data: bytes) -> bytes | None:
    """本机解压，返回**完整**内容。会话文件是多帧追加写的，必须解所有帧：
    compression.zstd(≥3.14) → zstandard → zstd 命令 → node（逐帧循环）。

    注意：node 的 zstdDecompressSync 和 python 的 ZstdDecompressor().decompress()
    默认只解第一帧（实测 55KB 的会话只得到 185 字节），所以 node 必须按帧循环解。
    """
    if _zstd is not None:
        try:
            return _zstd.decompress(data)          # 模块级函数：处理多帧
        except Exception:
            pass
    if _zstandard is not None:
        try:
            return _zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)).read()
        except Exception:
            pass
    for name in ("zstd", "zstdcat", "unzstd"):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            proc = subprocess.run([exe, "-dcq"], input=data, capture_output=True, timeout=30)
            if proc.returncode == 0 and proc.stdout:   # zstd 命令默认解所有帧
                return proc.stdout
        except Exception:
            continue
    exe = shutil.which("node")
    if exe:
        try:
            proc = subprocess.run([exe, "-e", _ZSTD_NODE_JS % "0"], input=data,
                                  capture_output=True, timeout=60)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except Exception:
            pass
    return None

HOME = Path.home()
HOSTS_FILE = Path(os.environ.get("DSH_ROUTE_HOSTS_FILE") or "/etc/hosts")   # 测试可覆盖
RUN_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "dsh-route"
RUN_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = Path(os.environ.get("DSH_ROUTE_CONFIG") or (HOME / ".config/dsh-route/config"))
LOG_PATH = RUN_DIR / "daemon.log"
KEY_FILE = Path(os.environ.get("DSH_ROUTE_KEY_FILE") or (HOME / ".config/dsh-route/deepseek.key"))
UI_FILE = Path(__file__).resolve().parent / "ui.html"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"          # 字体等静态资源
STATE_DIR = Path(os.environ.get("DSH_ROUTE_STATE") or (HOME / ".local/state/dsh-route"))
HISTORY_LIMIT = 2000                                             # 余额采样点上限
_UI_CACHE: dict[str, object] = {"mtime": 0.0, "text": ""}

DEFAULTS: dict[str, object] = {
    "BASE_PORT": 3080,
    "REMOTE_PORT": 3080,
    "REMOTE_PORTS": "3080,3081,3082",   # 候选远端端口，逐个验证
    "POLL": 5,
    "PROBE_TIMEOUT": 8,
    "CONTROL_PORT": 3090,
    "TOKEN_TTL": 90,
    "METRICS_POLL": 30,
    "SESSION_POLL": 45,
    "SESSION_LIMIT": 3,
    "USAGE_DAYS": 7,
    "POWERSHELL": "",
    "EXTRA_EXCLUDE": "",
    "DSH_HOME": "",
    "DEEPSEEK_API_KEY": "",
    "API_BASE": "https://api.deepseek.com",
    "STATUS_URL": "https://status.deepseek.com/",
    "PROXY": "",                # 形如 http://127.0.0.1:7890；本机直连受限时可用
    "HOST_ALIAS": "dsh.local",  # 好记的访问别名（安装时可写进 /etc/hosts）；留空则只用 localhost
}

SSH_OPTS = [
    "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "RemoteCommand=none",
    "-o", "RequestTTY=no", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR",
]
SSH_KEEPALIVE = [
    "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "TCPKeepAlive=yes",
    "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10",
]

# 逐个端口验证：只认 dsh web 的特征响应
PROBE_PORTS_SCRIPT = r'''
tmp=$(mktemp 2>/dev/null || echo /tmp/dshprobe.$$)
for p in "$@"; do
  [ -n "$p" ] || continue
  code=$(curl -s -m 3 -o "$tmp" -w '%{http_code}' "http://127.0.0.1:$p/" 2>/dev/null)
  if [ "$code" = "401" ] && grep -q "dsh web authentication required" "$tmp" 2>/dev/null; then
    echo "$p DSH 401"
  elif [ -n "$code" ] && [ "$code" != "000" ]; then
    echo "$p HTTP $code"
  else
    echo "$p DOWN -"
  fi
done
rm -f "$tmp" 2>/dev/null
'''

TOKEN_SCRIPT = r'''
# 1) 正在运行的 dsh web 进程：命令行 + 环境变量（不依赖 tmux）
if [ -d /proc ]; then
  for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    c=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    case "$c" in
      *dsh*web*|*web*dsh*) printf '%s\n' "$c"
                           tr '\0' '\n' < "$p/environ" 2>/dev/null | grep -i token || true ;;
    esac
  done
fi
# 2) 日志：dsh web 的启动输出（URL 里就带 token）
for f in "$HOME"/dsh-web*.log "$HOME"/dsh*.log "$HOME"/nohup.out "$HOME"/.dsh/logs/* \
         "${DSH_HOME:-}"/logs/* "${DSH_HOME:-}"/*.log /tmp/dsh*.log /tmp/dsh-web*; do
  [ -f "$f" ] && tail -c 200000 "$f" 2>/dev/null
done
# 3) tmux（装了就顺手捞一下，没装也照样能找到）
if command -v tmux >/dev/null 2>&1; then
  tmux ls -F '#{session_name}' 2>/dev/null | while read -r s; do
    tmux capture-pane -p -J -S -8000 -t "$s" 2>/dev/null
  done
fi
# 4) 兜底：ps 带参数（macOS 上没有 /proc）
ps -Ao args= 2>/dev/null | grep -i dsh | grep -i web || true
'''

USAGE_SCRIPT = r'''
for h in "${DSH_HOME:-}" "$HOME/dsh/home" "/gaojiawei/lgy/software/dsh/home" "$HOME/software/dsh/home" "$HOME/.dsh"; do
  [ -n "$h" ] || continue
  [ -d "$h/sessions" ] || continue
  find "$h/sessions" -name 'session.v4.jsonl.zstd' -printf '%T@\n' 2>/dev/null
done | head -3000
'''

SESSION_LIST_SCRIPT = r'''
root=""
for base in "${DSH_HOME:-}" "$HOME/dsh/home" "/gaojiawei/lgy/software/dsh/home" "$HOME/software/dsh/home" "$HOME/.dsh"; do
  [ -n "$base" ] || continue
  [ -d "$base/sessions" ] && { root="$base/sessions"; break; }
done
[ -n "$root" ] || exit 0
find "$root" -name 'session.v4.jsonl.zstd' -printf '%T@ %s %p\n' 2>/dev/null | sort -rn | head -"${1:-3}"
'''

# 让"读会话"在远端完成最省事：远端有 zstd 命令 / node（dsh 自带）/(带 zstd 的) python 都行。
# ⚠️ 会话文件是多帧追加写的（实测 26 帧），node 的 zstdDecompressSync 只解第一帧
#    （55KB 的会话只得到 185 字节），所以 node 必须按帧头魔数切分后逐帧解。
SESSION_READ_SCRIPT = r'''
p="$1"
[ -f "$p" ] || exit 2
if base64 --help 2>&1 | grep -q -- '-w'; then B64='base64 -w0'; else B64='base64'; fi
enc() { $B64 | tr -d '\n'; }
plain="${p%.zstd}"
[ -f "$plain" ] && { enc < "$plain"; exit 0; }

find_node() {
  if [ -n "${NODE_BIN:-}" ] && [ -x "$NODE_BIN" ]; then printf '%s' "$NODE_BIN"; return 0; fi
  n=$(command -v node 2>/dev/null) && { printf '%s' "$n"; return 0; }
  # dsh 自带的 node：DSH_HOME 的上一级 node/bin/node（实测 /gaojiawei/lgy/software/dsh/node/bin/node）
  for base in "${DSH_HOME:-}" "$HOME/dsh/home" "/gaojiawei/lgy/software/dsh/home" "$HOME/software/dsh/home" "$HOME/.dsh"; do
    [ -n "$base" ] || continue
    top="${base%/home}"
    for cand in "$top/node/bin/node" "$top/bin/node" "$top/../node/bin/node" "$base/node/bin/node"; do
      [ -x "$cand" ] && { printf '%s' "$cand"; return 0; }
    done
  done
  for cand in "$HOME"/.nvm/versions/node/*/bin/node /usr/local/bin/node /usr/bin/node /opt/*/bin/node \
              "$HOME"/.volta/bin/node "$HOME"/.local/share/fnm/node-versions/*/installation/bin/node \
              "$HOME"/.vscode-server/cli/servers/*/server/node "$HOME"/.local/bin/node; do
    [ -x "$cand" ] && { printf '%s' "$cand"; return 0; }
  done
  return 1
}

if command -v zstd >/dev/null 2>&1; then zstd -dcq -- "$p" 2>/dev/null | enc && exit 0; fi
if N=$(find_node); then
  out=$("$N" -e 'const fs=require("fs"),z=require("zlib");const b=fs.readFileSync(process.argv[1]);
const M=Buffer.from([0x28,0xb5,0x2f,0xfd]);let s=[],i=0;
while((i=b.indexOf(M,i))!==-1){s.push(i);i+=4}s.push(b.length);
let o=[],p=null;for(let k=0;k<s.length-1;k++){
const c=p?Buffer.concat([p,b.subarray(s[k],s[k+1])]):b.subarray(s[k],s[k+1]);
try{o.push(z.zstdDecompressSync(c));p=null}catch(e){p=c}}
process.stdout.write(Buffer.concat(o).toString("base64"))' "$p" 2>/dev/null)
  [ -n "$out" ] && { printf '%s' "$out"; exit 0; }
fi
for py in python3 python; do
  command -v "$py" >/dev/null 2>&1 || continue
  "$py" -c 'import base64,io,sys
data=open(sys.argv[1],"rb").read()
try:
    from compression import zstd as Z
    raw=Z.decompress(data)                       # 模块级函数：多帧
except Exception:
    try:
        import zstandard as Z
        raw=Z.ZstdDecompressor().stream_reader(io.BytesIO(data)).read()   # 多帧
    except Exception:
        raise SystemExit(4)
sys.stdout.write(base64.b64encode(raw).decode())' "$p" 2>/dev/null && exit 0
done
exit 5
'''

TAKES_VALUE = {"-b", "-c", "-D", "-e", "-F", "-i", "-J", "-L", "-l", "-m",
               "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%F %T')}] {msg}", flush=True)


def load_config() -> dict[str, object]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.is_file():
        for raw in CONFIG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in cfg:
                cfg[key] = value if isinstance(cfg[key], str) else int(value)
    return cfg


def candidate_ports(cfg: dict[str, object]) -> list[int]:
    ports: list[int] = []
    for chunk in str(cfg["REMOTE_PORTS"]).replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            ports.append(int(chunk))
    primary = int(cfg["REMOTE_PORT"])
    if primary in ports:
        ports.remove(primary)
    ports.insert(0, primary)
    seen: list[int] = []
    for p in ports:
        if p not in seen:
            seen.append(p)
    return seen


# ---------------------------------------------------------------- Windows 侧
def win_root() -> str | None:
    try:
        out = subprocess.run(["wslpath", "-u", "C:\\"], capture_output=True, text=True, timeout=5)
        root = out.stdout.strip()
        if out.returncode == 0 and root and Path(root).is_dir():
            return root.rstrip("/")
    except Exception:
        pass
    try:
        mounts = Path("/proc/mounts").read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in mounts:
        f = line.split()
        if len(f) >= 4 and f[2] in ("9p", "drvfs", "virtiofs") and ("path=C:" in f[3] or "path=c:" in f[3]):
            return f[1].rstrip("/")
    for line in mounts:
        f = line.split()
        if len(f) >= 2 and Path(f[1], "Windows/System32").is_dir():
            return f[1].rstrip("/")
    return None


def find_powershell(cfg: dict[str, object]) -> str | None:
    if cfg["POWERSHELL"]:
        return str(cfg["POWERSHELL"])
    for name in ("powershell.exe", "pwsh.exe"):
        found = shutil.which(name)
        if found:
            return found
    root = win_root()
    if not root:
        return None
    for rel in ("Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                "Program Files/PowerShell/7/pwsh.exe",
                "Program Files/PowerShell/7-preview/pwsh.exe"):
        candidate = Path(root) / rel
        if candidate.exists():
            return str(candidate)
    return None


def ps_script(ps: str | None, script: str, timeout: float = 30) -> str | None:
    """`-Command -` 从 stdin 执行（避免 interop 吃引号），显式按 UTF-8 解码。"""
    if not ps:
        return None
    prelude = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ErrorActionPreference='SilentlyContinue';"
    try:
        out = subprocess.run([ps, "-NoProfile", "-NoLogo", "-Command", "-"],
                             input=(prelude + script).encode("utf-8"), capture_output=True, timeout=timeout)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace").replace("\r", "")


def win_ssh_cmdlines(ps: str | None) -> list[str] | None:
    out = ps_script(ps, "Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | "
                        "Select-Object -ExpandProperty CommandLine")
    if out is None:
        return None
    return [l for l in out.splitlines() if l.strip()]


def win_ssh_peers(ps: str | None) -> list[str] | None:
    out = ps_script(ps, 'Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | '
                        'Where-Object { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName -eq "ssh" } | '
                        'ForEach-Object { "$($_.RemoteAddress):$($_.RemotePort)" }')
    if out is None:
        return None
    return [l.strip() for l in out.splitlines() if l.strip()]


LOCAL_SSH_NAMES = ("ssh", "ssh.exe")


def platform_kind() -> str:
    """wsl / macos / linux —— 决定用哪套"本机 ssh 进程"发现方式。"""
    if sys.platform == "darwin":
        return "macos"
    try:
        if "microsoft" in Path("/proc/version").read_text(errors="replace").lower():
            return "wsl"
    except OSError:
        pass
    return "linux"


PLATFORM = os.environ.get("DSH_ROUTE_PLATFORM") or platform_kind()


def proc_ssh_cmdlines() -> list[str]:
    """Linux / WSL：扫 /proc，找 comm=ssh 的进程。"""
    out: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() not in LOCAL_SSH_NAMES:
                continue
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            if cmd:
                out.append(cmd)
        except OSError:
            continue
    return out


def normalize_ps_args(args: str) -> str:
    """统一 ps 输出的命令行形态：有些 ps 的 args 不含 argv[0]，补一个 'ssh ' 前缀，
    这样 parse_target 的 'ssh ' 标记才能命中。"""
    if re.match(r"^ssh(\.exe)?(\s|$)", args):
        return args
    return "ssh " + args


def macos_ssh_cmdlines() -> list[str]:
    """macOS 没有 /proc，用 ps 取命令行；comm 可能是完整路径，只按 basename 判断。"""
    ps_cmd = os.environ.get("DSH_ROUTE_PS") or "ps"
    try:
        # 不能用 text=True：ps 输出里可能有非 UTF-8 字节（进程参数），会直接抛 UnicodeDecodeError
        proc = subprocess.run([ps_cmd, "-Ao", "comm=,args="], capture_output=True, timeout=10)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        comm, args = os.path.basename(parts[0]), parts[1].strip()
        if comm not in LOCAL_SSH_NAMES and not comm.startswith("ssh."):
            continue
        if comm in ("sshd", "ssh-agent", "ssh-keysign", "ssh-pkcs11-helper", "ssh-add"):
            continue
        if args:
            out.append(normalize_ps_args(args))
    return out


def local_ssh_cmdlines() -> list[str]:
    return macos_ssh_cmdlines() if PLATFORM == "macos" else proc_ssh_cmdlines()


def parse_target(line: str, extra_exclude: str) -> str | None:
    rest = None
    for marker in ('ssh.exe"', "ssh.exe ", "ssh "):
        idx = line.find(marker)
        if idx >= 0:
            rest = line[idx + len(marker):]
            break
    if rest is None:
        return None
    tokens = rest.split()
    if "-N" in tokens and "-L" in tokens:
        return None
    if "bash" in tokens and "-s" in tokens:
        return None
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            if token in TAKES_VALUE:
                skip = True
            continue
        token = token.split("@")[-1]
        if not token or token in ("wsl", "localhost") or token.startswith("127.") or token == "::1":
            return None
        if extra_exclude and token in extra_exclude.split():
            return None
        return token
    return None


def discover(cfg: dict[str, object]) -> tuple[list[tuple[str, str]], bool]:
    """返回 ([(目标, 来源标签)], 本轮发现是否可信)。

    本机扫描（/proc 或 ps）是主要口径；只有在 WSL 里才额外问 Windows 侧，
    因为 WSL 的 ssh 可能跑在宿主机上。两者都失败时才算"不可信"。
    """
    found: dict[str, str] = {}
    local_label = "WSL" if PLATFORM == "wsl" else ("本机(mac)" if PLATFORM == "macos" else "本机")

    local_ok = True
    local_lines = local_ssh_cmdlines()
    for line in local_lines:
        target = parse_target(line, str(cfg["EXTRA_EXCLUDE"]))
        if target:
            found.setdefault(target, local_label)

    windows_ok = True
    ps = None
    if PLATFORM == "wsl":
        ps = find_powershell(cfg)
        win_lines = win_ssh_cmdlines(ps)
        if ps is None or win_lines is None:
            windows_ok = False
            win_lines = []
        for line in win_lines:
            target = parse_target(line, str(cfg["EXTRA_EXCLUDE"]))
            if target:
                found.setdefault(target, "宿主")
    else:
        ps = None

    if not found and PLATFORM == "wsl":
        peers = win_ssh_peers(ps)
        if peers is None:
            windows_ok = False
            peers = []
        for peer in peers:
            ip = peer.rsplit(":", 1)[0]
            if ip.startswith("127.") or ip in ("::1", "localhost"):
                continue
            found.setdefault(peer, "宿主IP")

    reliable = local_ok and windows_ok
    STATE.last_probe = {
        "platform": PLATFORM,
        "local": "ps" if PLATFORM == "macos" else "/proc",
        "local_count": len(local_lines),
        "windows": (find_powershell(cfg) if PLATFORM == "wsl" else None) or "",
        "zstd": zstd_backend(),
        "reliable": reliable,
        "at": time.time(),
    }
    return list(found.items()), reliable



# ---------------------------------------------------------------- 远端操作
def split_target(target: str) -> tuple[str, list[str]]:
    m = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d+)$", target)
    if m:
        return m.group(1), ["-p", m.group(2)]
    return target, []


def ssh_run(target: str, script: str, args: list[str], timeout: float) -> tuple[int, str]:
    host, extra = split_target(target)
    try:
        # 同样按字节收、再容错解码：远端输出里出现非 UTF-8 字节不应该让整轮扫描失败
        proc = subprocess.run(["ssh", *extra, *SSH_OPTS, host, "bash", "-s", *args],
                              input=script.encode("utf-8"), capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, ""
    except Exception:
        return 1, ""


def probe_ports(host: str, ports: list[int], timeout: int) -> dict[int, str]:
    """返回 {端口: 'DSH'|'HTTP'|'DOWN'}，DSH 表示验证通过（dsh web 特征）。"""
    args = ["--"] + [str(p) for p in ports]
    code, out = ssh_run(host, PROBE_PORTS_SCRIPT, args, timeout)
    result: dict[int, str] = {p: "UNKNOWN" for p in ports}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            result[int(parts[0])] = parts[1]
    if code == 124:
        for p in ports:
            if result[p] == "UNKNOWN":
                result[p] = "TIMEOUT"
    return result


def fetch_token(host: str, remote_port: int) -> str | None:
    code, out = ssh_run(host, TOKEN_SCRIPT, [], timeout=25)
    if code != 0 and not out:
        return None
    tokens = re.findall(r"token=([A-Za-z0-9._-]{16,})", out)
    seen: list[str] = []
    for tok in reversed(tokens):
        if tok not in seen:
            seen.append(tok)
    check = 'curl -s -m 5 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$1/?token=$2"'
    for tok in seen[:10]:
        _code, out = ssh_run(host, check, ["--", str(remote_port), tok], timeout=20)
        if out.strip().endswith("303"):
            return tok
    return None


def parse_session(raw: bytes) -> dict:
    totals = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}
    title = ""
    model = ""
    last_ts = 0
    steps = 0
    turn_open = False
    approval_open = False
    first_user = ""
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("type", "")
        ts = d.get("time") or 0
        if ts > last_ts:
            last_ts = ts
        data = d.get("data") if isinstance(d.get("data"), dict) else {}
        if t == "assistant/message":
            u = data.get("usage")
            if isinstance(u, dict):
                steps += 1
                totals["input"] += u.get("inputTokens", 0) or 0
                totals["output"] += u.get("outputTokens", 0) or 0
                totals["cacheRead"] += u.get("cacheReadTokens", 0) or 0
                totals["cacheWrite"] += u.get("cacheWriteTokens", 0) or 0
                totals["total"] += u.get("totalTokens", 0) or 0
        elif t == "request/header":
            cfg = ((data.get("header") or {}).get("config") or {}) if isinstance(data.get("header"), dict) else {}
            model = cfg.get("model") or model
        elif t == "session/title":
            title = str(data.get("title") or data.get("text") or title)[:80]
        elif t == "turn/start":
            turn_open = True
        elif t == "turn/end":
            turn_open = False
        elif t == "approval/asked":
            approval_open = True
        elif t == "approval/decided":
            approval_open = False
        elif t == "user/message" and not first_user:
            msg = data.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        first_user = str(part.get("text") or "")[:60]
                        break
            elif isinstance(content, str):
                first_user = content[:60]
    status = "blocked" if approval_open else ("running" if turn_open else "idle")
    return {"title": title or first_user, "model": model, "tokens": totals, "steps": steps,
            "status": status, "last": (last_ts / 1000.0) if last_ts else 0.0}


SESSION_CACHE: dict[str, tuple[float, int, list[dict]]] = {}


def collect_sessions(host: str) -> list[dict]:
    now = time.time()
    cached = SESSION_CACHE.get(host)
    if cached and now - cached[0] < cached[1]:
        return cached[2]
    limit = int(CONFIG["SESSION_LIMIT"])
    _code, out = ssh_run(host, SESSION_LIST_SCRIPT, ["--", str(limit)], timeout=25)
    entries: list[tuple[float, int, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            entries.append((float(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    sessions: list[dict] = []
    for mtime, size, path in entries[:limit]:
        item = {"id": Path(path).parent.name.replace("session-", "")[:8], "path": path,
                "mtime": mtime, "size_bytes": size, "title": "", "model": "", "status": "unknown",
                "tokens": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
                "steps": 0, "last": mtime}
        if size <= 8_000_000:
            raw: bytes | None = None
            via = ""
            # 首选：让远端解压（远端有 zstd 命令或带 zstd 的 python 就行）
            _rc, b64 = ssh_run(host, SESSION_READ_SCRIPT, ["--", path], timeout=40)
            body = "".join(b64.split())
            if body:
                try:
                    raw = base64.b64decode(body)
                    via = "远端"
                except Exception:
                    raw = None
            # 会话文件是多帧追加写的：个别解码器只给第一帧（实测 185B vs 全量 55KB），
            # 所以结果不像"完整一行"时，本机再解一遍，取更长的那个。
            if raw is None or not raw.endswith(b"\n"):
                fetch = 'base64 -w0 < "$1" 2>/dev/null || base64 < "$1" | tr -d "\\n"'
                _rc2, raw_b64 = ssh_run(host, fetch, ["--", path], timeout=40)
                blob = "".join(raw_b64.split())
                if blob:
                    try:
                        alt = local_zstd_decompress(base64.b64decode(blob))
                    except Exception:
                        alt = None
                    if alt and (raw is None or len(alt) > len(raw)):
                        raw, via = alt, "本机"
            if raw:
                item.update(parse_session(raw))
                item["parsed"] = True
                item["via"] = via
            else:
                item["error"] = "会话文件解不开（远端/本机都缺 zstd：装个 zstd 命令行最省事）"
        sessions.append(item)
    # 空结果只短缓存，避免"启动时隧道还没建好"把空结果锁死 45 秒
    SESSION_CACHE[host] = (now, int(CONFIG["SESSION_POLL"]) if sessions else 8, sessions)
    return sessions


# ---------------------------------------------------------------- 隧道
class Tunnel:
    def __init__(self, host: str, remote_port: int, local_port: int):
        self.host = host
        self.remote_port = remote_port
        self.local_port = local_port
        self.pid: int | None = None
        self.since: float = 0.0
        self.restarts = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._supervise, daemon=True)

    @property
    def key(self) -> tuple[str, int]:
        return (self.host, self.remote_port)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass

    @property
    def alive(self) -> bool:
        if not self.pid:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def _supervise(self) -> None:
        backoff = 2
        host, extra = split_target(self.host)
        while not self._stop.is_set():
            started = time.time()
            cmd = ["ssh", *extra, "-N", *SSH_OPTS, *SSH_KEEPALIVE,
                   "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}", host]
            try:
                with LOG_PATH.open("a", encoding="utf-8") as fh:
                    proc = subprocess.Popen(cmd, stdout=fh, stderr=fh)
                    self.pid = proc.pid
                    self.since = time.time()
                    proc.wait()
            except Exception as exc:
                log(f"tunnel {self.host}:{self.remote_port} spawn failed: {exc}")
            self.pid = None
            if self._stop.is_set():
                return
            self.restarts += 1
            if time.time() - started >= 60:
                backoff = 2
            log(f"tunnel {self.host}:{self.remote_port} 断开，{backoff}s 后重连（已重连 {self.restarts} 次）")
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 30)


# ---------------------------------------------------------------- 状态
class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[dict] = []
        self.scanned_at: float = 0.0
        self.scanning = False
        self.ps_path: str | None = None
        self.last_probe: dict = {}


STATE = State()
METRICS: dict[str, object] = {}
METRICS_LOCK = threading.Lock()
TUNNELS: dict[tuple[str, int], Tunnel] = {}
TOKENS: dict[tuple[str, int], tuple[str | None, float]] = {}
CONFIG = load_config()
MISSES: dict[tuple[str, int], int] = {}
MISS_LIMIT = 3
STARTED_AT = time.time()


def api_key() -> str:
    if CONFIG["DEEPSEEK_API_KEY"]:
        return str(CONFIG["DEEPSEEK_API_KEY"]).strip()
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"].strip()
    try:
        if KEY_FILE.is_file():
            return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def save_api_key(key: str) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = key.strip()
    if not key:
        try:
            KEY_FILE.unlink()
        except OSError:
            pass
        return
    fd = os.open(str(KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def proxy_url() -> str:
    return str(CONFIG.get("PROXY") or "").strip() or \
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""


REASON_LABEL = {"tls": "TLS 握手失败", "tls-reset": "TLS 被重置", "tls-cert": "证书校验失败",
                "dns": "域名解析失败", "timeout": "连接超时", "curl": "curl 失败", "other": "网络错误"}


def classify_net_error(exc: BaseException) -> tuple[str, str]:
    """把网络异常翻译成"人话 + 可操作提示"，面板直接用。"""
    inner = getattr(exc, "reason", None) or exc
    text = f"{type(inner).__name__}: {inner}"
    up = text.upper()
    if isinstance(inner, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in up:
        return "tls-cert", "本机不信任该站点的 CA（macOS 自带 python 常见，可装证书或换 Homebrew python）"
    if ("CONNECTION RESET" in up) or isinstance(inner, ConnectionResetError) or "ERRNO=54" in up:
        return "tls-reset", ("连接在 TLS 握手阶段被重置：常见于 CDN/WAF 拒绝本机客户端的 TLS 指纹，"
                             "或本地网络/防火墙拦截；可尝试设置 PROXY 或换一个能访问的网络")
    if isinstance(inner, ssl.SSLError) or up.startswith("SSL"):
        return "tls", "TLS 握手失败（本机 TLS 栈或网络拦截）"
    if isinstance(inner, socket.gaierror):
        return "dns", "域名解析失败（检查 DNS）"
    if isinstance(inner, (TimeoutError, socket.timeout)) or "TIMED OUT" in up:
        return "timeout", "连接超时（网络不通或被墙）"
    return "other", text[:160]


def classify_curl_error(stderr: str) -> tuple[str, str]:
    """curl 的报错也翻译成人话（curl 与 python 的 TLS 栈不同，常常只有一个失败）。"""
    low = stderr.lower()
    if "connection reset" in low:
        return "tls-reset", ("连接在 TLS 握手阶段被重置：常见于 CDN/WAF 拒绝本机客户端的 TLS 指纹，"
                             "或本地网络/防火墙拦截；可尝试设置 PROXY 或换一个能访问的网络")
    if "certificate" in low or "ssl certificate problem" in low:
        return "tls-cert", "证书校验失败（本机不信任该站点的 CA）"
    if "ssl" in low or "tls" in low:
        return "tls", "TLS 握手失败（本机 TLS 栈或网络拦截）"
    if "could not resolve" in low or "name or service not known" in low:
        return "dns", "域名解析失败（检查 DNS）"
    if "timed out" in low:
        return "timeout", "连接超时（网络不通或被墙）"
    return "curl", stderr[:160]


def fetch_text(url: str, headers: dict[str, str] | None = None,
               timeout: float = 15) -> tuple[str, str, str, str]:
    """抓文本：先 python(urllib)，再用系统 curl 兜底（两者 TLS 栈不同，常常一个能过一个不能）。

    返回 (正文, 用的手段, 错误类型, 错误说明)。
    """
    hdrs = {"user-agent": UA, **(headers or {})}
    first: tuple[str, str] = ("other", "")
    try:
        req = urllib.request.Request(url, headers=hdrs)
        proxy = proxy_url()
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            resp = opener.open(req, timeout=timeout)
        else:
            resp = urllib.request.urlopen(req, timeout=timeout)
        with resp:
            return resp.read().decode("utf-8", "replace"), "python", "", ""
    except Exception as exc:
        first = classify_net_error(exc)

    curl = shutil.which("curl")
    if curl:
        cmd = [curl, "-sS", "-L", "--max-time", str(int(timeout)), "-A", UA]
        for k, v in hdrs.items():
            if k.lower() == "user-agent":
                continue
            cmd += ["-H", f"{k}: {v}"]
        proxy = proxy_url()
        if proxy:
            cmd += ["-x", proxy]
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout.decode("utf-8", "replace"), "curl", "", ""
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            msg = (err[-1] if err else f"curl exit {proc.returncode}")[:200]
            kind, hint = classify_curl_error(msg)
            return "", "curl", kind, f"{msg}｜{hint}"
        except Exception as exc:
            return "", "curl", *classify_net_error(exc)
    return "", "none", *first


def fetch_json(url: str, key: str = "", timeout: float = 10) -> tuple[int, dict | None, float, str]:
    """JSON 接口：urllib 优先，失败用 curl 兜底（解决"某平台 python TLS 被拦"的情况）。"""
    began = time.perf_counter()
    headers = {"accept": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(url, headers=headers)
        proxy = proxy_url()
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            resp = opener.open(req, timeout=timeout)
        else:
            resp = urllib.request.urlopen(req, timeout=timeout)
        with resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, _loads(body), (time.perf_counter() - began) * 1000, ""
    except urllib.error.HTTPError as exc:
        try:
            data = _loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            data = None
        return exc.code, data, (time.perf_counter() - began) * 1000, ""
    except Exception as exc:
        kind, msg = classify_net_error(exc)

    text, via, ckind, cmsg = fetch_text(url, {"accept": "application/json"}, timeout)
    if text:
        return 200, _loads(text), (time.perf_counter() - began) * 1000, f"经 {via}"
    return 0, None, (time.perf_counter() - began) * 1000, cmsg or msg


def _loads(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def http_json(url: str, key: str = "", timeout: float = 10) -> tuple[int, dict | None, float]:
    status, data, latency, _note = fetch_json(url, key, timeout)
    return status, data, latency


def collect_balance() -> dict:
    key = api_key()
    base = str(CONFIG["API_BASE"]).rstrip("/")
    if not key:
        return {"configured": False, "ok": False, "checked_at": time.time()}
    status, data, latency = http_json(f"{base}/user/balance", key)
    if status == 200 and isinstance(data, dict):
        infos = data.get("balance_infos") or []
        info = infos[0] if infos else {}
        return {"configured": True, "ok": True, "status": status, "latency_ms": round(latency),
                "available": bool(data.get("is_available")), "currency": info.get("currency", ""),
                "total": info.get("total_balance", ""), "granted": info.get("granted_balance", ""),
                "topped_up": info.get("topped_up_balance", ""), "checked_at": time.time()}
    message = ""
    if isinstance(data, dict):
        message = str((data.get("error") or {}).get("message", ""))[:160]
    return {"configured": True, "ok": False, "status": status, "latency_ms": round(latency),
            "error": message or f"HTTP {status}", "checked_at": time.time()}


def collect_api() -> dict:
    key = api_key()
    base = str(CONFIG["API_BASE"]).rstrip("/")
    status, data, latency = http_json(f"{base}/models", key)
    models: list[str] = []
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        models = [str(m.get("id", "")) for m in data["data"] if isinstance(m, dict)][:12]
    ok = status in (200, 401)
    with METRICS_LOCK:
        hist = list((METRICS.get("api") or {}).get("history") or [])   # type: ignore[union-attr]
    if ok:
        hist.append(round(latency))
        hist = hist[-40:]
    return {"ok": ok, "status": status, "latency_ms": round(latency), "models": models,
            "authed": status == 200, "history": hist, "checked_at": time.time()}


def collect_status_page() -> dict:
    url = str(CONFIG["STATUS_URL"])
    raw, via, kind, err = fetch_text(url, {"accept": "text/html"}, timeout=15)
    if not raw:
        return {"ok": False, "reason": REASON_LABEL.get(kind, "网络错误"),
                "error": (err or "抓取失败")[:90], "error_kind": kind, "via": via,
                "checked_at": time.time()}

    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = html_mod.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()

    overall = ""
    m = re.search(r"Powered by(?:\s+DeepSeek Status)?\s+(.+?)\s+Event calendar", text)
    if m:
        overall = m.group(1).strip()
    healthy = bool(re.search(r"running smoothly|operating as expected|All Systems Operational", overall, re.I))
    if not overall:
        healthy = bool(re.search(r"running smoothly|operating as expected|All Systems Operational", text, re.I))
        overall = "一切正常运行" if healthy else "状态未知"

    components = []
    for mm in re.finditer(r'class="font-medium text-sm truncate">([^<]{2,80})</span>(.{0,1400}?)'
                          r'([0-9]{1,3}\.[0-9]{2})% uptime', raw, re.S):
        name = html_mod.unescape(mm.group(1)).strip()
        if name and all(c["name"] != name for c in components):
            components.append({"name": name, "uptime": mm.group(3) + "%"})

    incidents = []
    for kw in ("Investigating", "Identified", "Monitoring", "In progress", "Scheduled maintenance",
               "Partial Outage", "Major Outage", "Degraded Performance"):
        for mm in re.finditer(kw, text):
            snippet = text[max(0, mm.start() - 60): mm.start() + 120].strip()
            if snippet not in incidents:
                incidents.append(snippet)
    return {"ok": True, "overall": overall or "状态未知", "healthy": healthy, "via": via,
            "components": components[:8], "incidents": incidents[:3], "checked_at": time.time()}


def usage_samples() -> dict:
    days = int(CONFIG["USAGE_DAYS"])
    now = time.time()
    buckets: dict[str, int] = {}
    sources: dict[str, int] = {}
    latest = 0.0

    def add(mtime: float, source: str) -> None:
        nonlocal latest
        if not mtime:
            return
        latest = max(latest, mtime)
        if now - mtime > days * 86400:
            return
        day = time.strftime("%Y-%m-%d", time.localtime(mtime))
        buckets[day] = buckets.get(day, 0) + 1
        sources[source] = sources.get(source, 0) + 1

    for base in glob.glob("/mnt/c/Users/*/.dsh/sessions") + glob.glob(str(HOME / ".dsh/sessions")):
        for path in glob.glob(base + "/**/session.v4.jsonl.zstd", recursive=True):
            try:
                add(os.path.getmtime(path), "Windows")
            except OSError:
                pass
    hosts = sorted({h for h, _p in TUNNELS})
    for host in hosts:
        code, out = ssh_run(host, USAGE_SCRIPT, [], timeout=20)
        if code != 0:
            continue
        for line in out.splitlines():
            try:
                add(float(line.strip()), host)
            except ValueError:
                pass

    order = [time.strftime("%Y-%m-%d", time.localtime(now - i * 86400)) for i in range(days - 1, -1, -1)]
    return {"days": [{"date": d, "count": buckets.get(d, 0)} for d in order],
            "total": sum(buckets.values()), "sources": sources, "last_active": latest, "checked_at": now}


def history_path() -> Path:
    return STATE_DIR / "balance-history.json"


def load_history() -> list:
    try:
        data = json.loads(history_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[list[float]] = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                out.append([float(item[0]), float(item[1])])
            except (TypeError, ValueError):
                continue
    return out[-HISTORY_LIMIT:]


def record_balance(balance: dict) -> list:
    """余额采样：给"余额走势"图表用，落盘以便重启后保留。"""
    with METRICS_LOCK:
        hist = METRICS.get("balance_history")
    hist = list(hist) if isinstance(hist, list) else load_history()
    if balance.get("ok"):
        try:
            hist.append([time.time(), float(str(balance.get("total") or 0))])
        except (TypeError, ValueError):
            pass
        hist = hist[-HISTORY_LIMIT:]
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            history_path().write_text(json.dumps(hist), encoding="utf-8")
        except OSError:
            pass
    return hist[-HISTORY_LIMIT:]


def metrics_once() -> None:
    balance = collect_balance()
    api = collect_api()
    status_page = collect_status_page()
    usage = usage_samples()
    sessions = []
    for host in sorted({h for h, _p in TUNNELS})[:8]:      # 所有在线目标的会话
        try:
            got = collect_sessions(host)
        except Exception as exc:
            log(f"sessions({host}) failed: {exc!r}")
            got = []
        if got:
            sessions.append({"host": host, "sessions": got})
    with METRICS_LOCK:
        METRICS["balance"] = balance
        prev = METRICS.get("api")
        if isinstance(prev, dict) and prev.get("ok") and not api.get("history"):
            api["history"] = prev.get("history") or []
        METRICS["api"] = api
        METRICS["status_page"] = status_page
        METRICS["usage"] = usage
        METRICS["sessions"] = sessions
    METRICS["balance_history"] = record_balance(balance)


def metrics_loop() -> None:
    while True:
        try:
            metrics_once()
        except Exception as exc:
            log(f"metrics failed: {exc!r}")
        with METRICS_LOCK:
            got_sessions = bool(METRICS.get("sessions"))
        delay = max(10, int(CONFIG["METRICS_POLL"]))
        # 刚启动时隧道可能还没建好，会话读不到就隔几秒重试
        if not got_sessions and TUNNELS and time.time() - STARTED_AT < 180:
            delay = 8
        time.sleep(delay)


# ---------------------------------------------------------------- 扫描与端口分配
def allocate_local_ports(keys: list[tuple[str, int]]) -> dict[tuple[str, int], int]:
    """给每个"主机:远端端口"分配本地端口：优先沿用已有分配，其余从 BASE_PORT 起顺延。

    没有"主口"概念，分配只依赖稳定顺序（主机名 + 候选端口顺序），
    这样新机器加入时不会把已有机器的端口全部打乱。
    """
    base = int(CONFIG["BASE_PORT"])
    ports: dict[tuple[str, int], int] = {}
    used: set[int] = set()
    for key in keys:                                     # 第一轮：沿用旧分配
        existing = TUNNELS.get(key)
        if existing and existing.local_port not in used:
            ports[key] = existing.local_port
            used.add(existing.local_port)
    nxt = base
    for key in keys:                                     # 第二轮：空位补最小可用端口
        if key in ports:
            continue
        while nxt in used:
            nxt += 1
        ports[key] = nxt
        used.add(nxt)
    return ports


def tunnel_latency(port: int | None) -> int | None:
    if not port:
        return None
    try:
        out = subprocess.run(["curl", "-s", "-m", "3", "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
                              f"http://127.0.0.1:{port}/"], capture_output=True, text=True, timeout=6)
        parts = out.stdout.split()
        if len(parts) == 2 and parts[0] != "000":
            return round(float(parts[1]) * 1000)
    except Exception:
        pass
    return None


def scan_once() -> None:
    ports_cfg = candidate_ports(CONFIG)
    targets, reliable = discover(CONFIG)
    target_hosts = {t[0] for t in targets}
    if not reliable:
        for host in sorted({h for h, _p in TUNNELS}):
            if host not in target_hosts:
                targets.append((host, "已知"))
                target_hosts.add(host)

    with ThreadPoolExecutor(max_workers=8) as pool:
        probed = list(pool.map(lambda t: probe_ports(t[0], ports_cfg, int(CONFIG["PROBE_TIMEOUT"])), targets))

    host_ports: dict[str, dict[int, str]] = {}
    for (host, source), result in zip(targets, probed):
        host_ports[host] = result

    # 每台主机：验证通过的 dsh 端口（按候选顺序）
    dsh_ports: dict[str, list[int]] = {}
    for host, result in host_ports.items():
        dsh_ports[host] = [p for p in ports_cfg if result.get(p) == "DSH"]
    up_hosts = [h for h, ps in dsh_ports.items() if ps]

    # 期望的隧道集合：所有验证通过的端口，按"主机名 + 候选端口顺序"稳定排序
    keys: list[tuple[str, int]] = []
    for host in sorted(up_hosts):
        for port in dsh_ports[host]:
            keys.append((host, port))
    keys.sort(key=lambda k: (k[0], ports_cfg.index(k[1]) if k[1] in ports_cfg else 99))

    local_ports = allocate_local_ports(keys)

    for key in keys:
        tunnel = TUNNELS.get(key)
        if tunnel is None or tunnel.local_port != local_ports[key]:
            if tunnel:
                tunnel.stop()
            tunnel = Tunnel(key[0], key[1], local_ports[key])
            TUNNELS[key] = tunnel
            tunnel.start()
    for key in list(TUNNELS):
        if key in local_ports:
            MISSES.pop(key, None)
            continue
        if not reliable:
            continue
        MISSES[key] = MISSES.get(key, 0) + 1
        if MISSES[key] >= MISS_LIMIT:
            MISSES.pop(key, None)
            TUNNELS.pop(key).stop()
            TOKENS.pop(key, None)

    now = time.time()
    for key in keys:
        token, checked = TOKENS.get(key, (None, 0.0))
        if now - checked > int(CONFIG["TOKEN_TTL"]):
            fresh = fetch_token(key[0], key[1])
            TOKENS[key] = (fresh if fresh else token, now)

    rows = []
    for host, source in targets:
        result = host_ports.get(host, {})
        forwards = []
        for port in dsh_ports.get(host, []):
            tunnel = TUNNELS.get((host, port))
            token, _ = TOKENS.get((host, port), (None, 0.0))
            forwards.append({"remote_port": port, "local_port": local_ports.get((host, port)),
                             "kind": "DSH", "forwarded": True, "token": token,
                             "alive": bool(tunnel and tunnel.alive),
                             "uptime": int(time.time() - tunnel.since) if tunnel and tunnel.since else 0,
                             "restarts": tunnel.restarts if tunnel else 0,
                             "latency_ms": tunnel_latency(local_ports.get((host, port)))})
        for port, kind in sorted(result.items()):
            if kind == "HTTP":
                forwards.append({"remote_port": port, "local_port": None, "kind": "HTTP",
                                 "forwarded": False, "token": None, "alive": False,
                                 "uptime": 0, "restarts": 0, "latency_ms": None})
        if not forwards:
            continue
        lat = [f["latency_ms"] for f in forwards if f["latency_ms"] is not None]
        rows.append({"host": host, "source": source,
                     "remote_ports": [f["remote_port"] for f in forwards if f["forwarded"]],
                     "forwards": forwards, "latency_ms": min(lat) if lat else None,
                     "restarts": sum(f["restarts"] for f in forwards)})
    rows.sort(key=lambda r: r["host"])
    with STATE.lock:
        STATE.rows = rows
        STATE.scanned_at = now
        STATE.ps_path = find_powershell(CONFIG)


def scan_loop() -> None:
    while True:
        try:
            with STATE.lock:
                STATE.scanning = True
            scan_once()
        except Exception as exc:
            log(f"scan failed: {exc!r}")
        finally:
            with STATE.lock:
                STATE.scanning = False
        time.sleep(int(CONFIG["POLL"]))


# ---------------------------------------------------------------- HTTP
def render_index() -> str:
    text = ""
    try:
        st = UI_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)          # 用 ns+长度做键，避免同 mtime 误判
        if _UI_CACHE["mtime"] != key:
            _UI_CACHE["text"] = UI_FILE.read_text(encoding="utf-8")
            _UI_CACHE["mtime"] = key
        text = str(_UI_CACHE["text"] or "")
    except OSError:
        text = ""
    if not text:
        text = "<!doctype html><meta charset='utf-8'><h1>dsh-route</h1><p>ui.html 缺失</p>"
    return (text.replace("__CONTROL__", str(int(CONFIG["CONTROL_PORT"])))
                .replace("__BASE__", str(int(CONFIG["BASE_PORT"]))))


class V6Server(ThreadingHTTPServer):
    """只监听 ::1 的 IPv6 回环（避免和 127.0.0.1 抢同一端口）。"""
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except OSError:
            pass
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    server_version = "dsh-route/5.0"

    def _send_static(self, target: Path) -> None:
        ctype = {".woff2": "font/woff2", ".woff": "font/woff", ".css": "text/css",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}.get(
                    target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("cache-control", "public, max-age=604800")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                      # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, render_index().encode(), "text/html; charset=utf-8")
            return
        # 浏览器默认会来要 /favicon.ico；apple-touch-icon 给"固定标签页/加到桌面"用
        if parsed.path in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            target = ASSETS_DIR / os.path.basename(parsed.path)
            if parsed.path.startswith("/apple-touch-icon") and not target.is_file():
                target = ASSETS_DIR / "apple-touch-icon.png"
            if target.is_file():
                self._send_static(target)
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        if parsed.path.startswith("/assets/"):
            name = os.path.basename(parsed.path)
            target = ASSETS_DIR / name
            if name and target.is_file():
                self._send_static(target)
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        if parsed.path == "/api/status":
            with STATE.lock:
                payload = {"scanned_at": STATE.scanned_at,
                           "scanning": STATE.scanning, "base_port": int(CONFIG["BASE_PORT"]),
                           "candidate_ports": candidate_ports(CONFIG),
                           "control_port": int(CONFIG["CONTROL_PORT"]),
                           "ps_path": STATE.ps_path, "platform": PLATFORM,
                           "probe": STATE.last_probe, "rows": STATE.rows,
                           **friendly_url()}
            self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/metrics":
            with METRICS_LOCK:
                payload = dict(METRICS)
            payload["uptime"] = int(time.time() - STARTED_AT)
            payload["poll"] = int(CONFIG["POLL"])
            payload["metrics_poll"] = int(CONFIG["METRICS_POLL"])
            payload["key_configured"] = bool(api_key())
            payload["key_file"] = str(KEY_FILE)
            payload["base"] = str(CONFIG["API_BASE"])
            self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if parsed.path == "/refresh":
            threading.Thread(target=scan_once, daemon=True).start()
            threading.Thread(target=metrics_once, daemon=True).start()
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:                     # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/key":
            length = int(self.headers.get("content-length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            key = str(body.get("key") or "")
            try:
                save_api_key(key)
            except Exception as exc:
                self._send(500, json.dumps({"ok": False, "error": str(exc)[:120]}).encode(),
                           "application/json; charset=utf-8")
                return
            threading.Thread(target=metrics_once, daemon=True).start()
            self._send(200, json.dumps({"ok": True, "configured": bool(api_key())}).encode(),
                       "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, *args) -> None:
        return


def host_alias() -> str:
    """好记的访问别名，例如 dsh.local（安装时可写进 /etc/hosts）。"""
    return str(CONFIG.get("HOST_ALIAS") or "").strip()


def alias_in_hosts(alias: str) -> bool:
    if not alias:
        return False
    try:
        for line in HOSTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2 and alias in parts[1:]:
                return True
    except OSError:
        pass
    return False


def friendly_url() -> dict:
    """给界面/命令行用的访问地址：优先 hosts 别名，其次 localhost（双栈已监听）。"""
    port = int(CONFIG["CONTROL_PORT"])
    alias = host_alias()
    local = "http://localhost:%d/" % port
    if alias and alias_in_hosts(alias):
        return {"url": "http://%s:%d/" % (alias, port), "url_local": local,
                "host_alias": alias, "alias_ok": True}
    return {"url": local, "url_local": local, "url_ip": "http://127.0.0.1:%d/" % port,
            "host_alias": alias, "alias_ok": False}


def main() -> int:
    try:                                     # 清掉历史"主口"记录
        (RUN_DIR / "selected").unlink()
    except OSError:
        pass
    log(f"dsh-route 启动：本地端口从 {CONFIG['BASE_PORT']} 起，候选远端端口 {candidate_ports(CONFIG)}，"
        f"控制页 {CONFIG['CONTROL_PORT']}，扫描 {CONFIG['POLL']}s，指标 {CONFIG['METRICS_POLL']}s，"
        f"API key {'已配置' if api_key() else '未配置'}")
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=metrics_loop, daemon=True).start()
    port = int(CONFIG["CONTROL_PORT"])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    # 顺便监听 ::1：有些系统把 localhost 解析成 IPv6，这样 http://localhost:3090/ 也能打开
    try:
        v6 = V6Server(("::1", port), Handler)
        threading.Thread(target=v6.serve_forever, daemon=True).start()
    except OSError as exc:
        log(f"IPv6 回环监听跳过（不影响 127.0.0.1）：{exc}")
    info = friendly_url()
    log(f"访问地址：{info['url']}"
        + ("" if info["alias_ok"] else f"（未配置 hosts 别名；127.0.0.1:{port} 一直可用）"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for tunnel in TUNNELS.values():
            tunnel.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
