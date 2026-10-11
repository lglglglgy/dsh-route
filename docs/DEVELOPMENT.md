# 开发与实现说明

面向贡献者与维护者。用户向的说明在 [README](../README.md)。

## 仓库结构

```
install.sh                 安装 / 升级 / 卸载入口（幂等；卸载优先交给下面的卸载器）
uninstall.sh               卸载入口的薄封装（等价于 ./install.sh --uninstall）
dist.sh                    构建 dist/*.run 与 *.tar.gz（.run = shell 桩 + tar.gz 载荷）
bin/dsh-route              命令行客户端（status/open/url/key/where/host-alias/服务控制/uninstall）
bin/dsh-route-uninstall    卸载器（安装时烧入本次安装的路径并放进 PATH，读 MANIFEST 精确删除）
src/daemon.py              守护进程（发现 / 探测 / 隧道 / 会话 / 指标 / HTTP 服务）
src/ui.html                控制台页面（单文件，含粒子标题、动态背景、路由台）
src/assets/                字体与图标（woff2 + favicon.svg/png/ico + apple-touch-icon）
service/                   systemd（user / system）与 launchd 三套服务定义模板
selfextract/stub.sh.in     .run 的自解压桩模板
config/config.example      配置模板（安装时不覆盖已有配置）
tests/                     见下
.github/workflows/         ci.yml（测试）、release.yml（打 tag 自动发版）
docs/                      本文件与控制台截图
```

## 本地开发

```bash
./install.sh          # 或 bash install.sh：装到 ~/.local/share/dsh-route 并重启服务
dsh-route log         # 看日志（systemd 用 journalctl，launchd 看 ~/Library/Logs/dsh-route.log）
make test check       # 见下
```

改前端只需改 `src/ui.html`（页面由守护进程现场读文件，刷新即生效）；改后端改 `src/daemon.py` 后 `dsh-route restart`。

## 测试

| 命令 | 覆盖 | 是否需要运行中的服务 |
| --- | --- | --- |
| `bash tests/release-check.sh dist/dsh-route-<ver>.run` | **产物闸门**：把构建出的 `.run` 解包 → 沙箱安装 → dry-run → 真卸载 → purge（12 项，全程隔离在临时目录） | 否 |
| `bash tests/platform-test.sh` | 平台分支：macOS `ps` 发现（桩）、launchd plist 渲染与 XML 校验、systemd 系统 unit 模板、无服务管理器兜底真启停、清单驱动卸载与隔离性 | 否 |
| `bash tests/smoke.sh` | 服务状态、控制页、字体、路径穿越防护、API 结构、每个转发端口实测 | 是 |
| `python3 tests/lint-braces.py .` | `$VAR` 紧邻非 ASCII 的写法（见下"兼容性坑"）；`--fix` 自动修 | 否 |
| `python3 tests/zstd-check.py` | 多帧 zstd 能否完整解开（见下"会话解析"） | 否 |

CI（`.github/workflows/ci.yml`）在 Linux 与 macOS 两边跑：语法检查 → lint-braces → 多帧 zstd 回归 →
shellcheck（只把 error 级当失败）→ 构建 → 产物自检 → 平台分支测试。冒烟测试需要真实服务，不进 CI。

## 发布流程

```bash
$EDITOR VERSION CHANGELOG.md      # 版本号与变更记录
git add -A && git commit -m "release: v0.1.5"
git tag v0.1.5 && git push origin main --tags
```

`release.yml` 会自动：校验 `tag == VERSION` → `dist.sh` 构建 → 跑产物自检 → `sha256sum -c` 自检 →
建 GitHub Release（附 `.run`、`.tar.gz` 及各自 `.sha256`，正文带下载表与安装命令）。

## 实现要点

### 发现

不依赖 ssh config，只看**当前活着的 ssh 进程**：

- Linux/WSL：扫 `/proc/*/cmdline`（`comm == ssh`）；
- macOS 没有 `/proc`，用 `ps -Ao comm=,args=`，`comm` 可能是完整路径、`args` 可能不含 argv[0]，
  两个形态都要兼容，并过滤 `sshd` / `ssh-agent` / `ssh-keysign` 之类，跳过 `-N -L` 隧道进程；
- WSL 额外读 Windows 侧 `ssh.exe` 的命令行（宿主机上发起的 ssh 也能看到），必要时用网络对端兜底。

`discover()` 返回 `(目标列表, 是否可信)`：本机探测成功即视为可信；只有两边都失败才不可信——
不可信时不会拆除已有隧道（避免抖动），`/api/status` 的 `probe` 会显示平台、探测方式与计数。

### 探测与转发

候选端口每个跑一段远端脚本，要求返回 **401 + `dsh web authentication required`** 才建隧道；
别的 HTTP 服务标记为"非 dsh，未转发"。每个"主机:远端端口"一条独立 `ssh -N -L` 隧道，
由 `Tunnel` 线程看护，断开按 2→30 秒退避重连。本地端口在 `BASE_PORT` 起分配，优先复用旧分配。

### 会话解析（多帧 zstd 的坑）

`session.v4.jsonl.zstd` 是 dsh **追加写**的，一个文件里可能有几十个 zstd 帧
（实测 21200 字节 / 26 帧）。**只解第一帧会得到 185 字节**（会话头），表现为"会话在但 token 全是 0"：

- node 的 `zstdDecompressSync(buf)` 与 python 的 `ZstdDecompressor().decompress(buf)` 都只解第一帧；
- python 的模块级 `zstd.decompress(buf)`、`zstandard` 的 `stream_reader`、`zstd -dc` 命令能解全部帧。

所以解析分两级：

1. **远端**（首选）：`zstd` 命令 → **dsh 自带的 node**（按帧头魔数 `28 B5 2F FD` 切分后逐帧解，
   解不开的段与下一段合并重试） → 带 zstd 的 python；
2. **本机兜底**：`compression.zstd`(3.14+) → `zstandard` → `zstd` 命令 → node（同样逐帧）。

结果不以换行结尾时视为可能被截断，会换另一侧再解一遍取更长的。`tests/zstd-check.py` 守这个回归。

### dsh web token

按顺序找：① 运行中的 dsh web 进程命令行与环境变量（`/proc`）→ ② 各类日志
（`~/dsh*.log`、`~/nohup.out`、`$DSH_HOME/**/*.log`、`/tmp/dsh*.log`…）→ ③ tmux 面板（有才用）→ ④ `ps`。
候选 token 会真的 curl 复验一遍，**只有返回 303 的才算数**，所以不依赖 tmux、也不会拿错。

### 访问别名与权限模型

别名（默认 `dsh.local`，配置项 `HOST_ALIAS`）写进 hosts 时带 `# dsh-route` 标记：

- 安装时：`/etc/hosts` 可写就直接写；能免密 sudo 就用 sudo；**交互式**安装会提示输入一次密码；
  WSL 里还会触发一次 **UAC** 写 Windows 的 hosts（`--windows-hosts` 强制、`--no-windows-hosts` 跳过）；
- 运行时：`ensure_host_alias()` 在启动时与每 5 分钟检查一次，不在就补。
  **以 root 运行（`./install.sh --system`）时这一步全自动**，WSL 每次启动重建 `/etc/hosts` 也能被补回；
  用户级服务没有写权限，只在 `status` 与页面上说明原因；
- 卸载：清单里记的 hosts 行（含 Windows 的）一并清掉。

`friendly_url()` 决定界面与 CLI 显示哪个地址（别名生效就用别名，否则 `localhost`），
控制页**同时监听 127.0.0.1 与 ::1**，所以 `localhost` 在把 localhost 解析成 IPv6 的系统上也能开。

> 注意：从 **Windows 浏览器**访问 WSL 里的服务时，Windows 把 `localhost` 解析成 `::1`（WSL 的转发是 IPv4），
> 所以要用 `127.0.0.1:3090` 或 Windows hosts 里的别名。

### 兼容性坑（都被 CI 或真机抓过）

- **bash 3.2 + UTF-8 locale**：`$VAR` 紧邻中文/全角字符时，macOS 自带 bash 3.2 会把多字节字符的首字节
  当成变量名的一部分，配合 `set -u` 直接 `unbound variable`。一律写 `${VAR}（…）`，
  由 `tests/lint-braces.py` + CI 步骤守住。
- **`ps` 输出的编码**：`ps -Ao comm=,args=` 里可能夹带非 UTF-8 字节，`subprocess.run(..., text=True)`
  会抛 `UnicodeDecodeError` 并被吞掉（表现为"永远发现不到机器"）。统一按字节收 + `decode(errors="replace")`。
- **systemd 日志路径**：不要用 `StandardOutput=append:%t/...`——systemd 打开该文件比创建目录更早，
  运行目录被删过（例如刚卸载又重装）会以 `status=209/STDOUT` 永久启动失败。日志走 journal，
  文件日志由守护进程自己写。
- **macOS 没有 `setsid`**：无服务管理器的兜底启动要"有 setsid 用 setsid，否则子 shell + nohup"。
- **BSD 工具差异**：`sed -i` 需要后缀参数、`shasum -a 256` 代替 `sha256sum`、`date -Is` 不可用——
  脚本里都做了兼容处理。

### 状态页（status.deepseek.com）

有些网络里该站会在 TLS 握手阶段被 CDN/WAF 重置（按客户端 TLS 指纹拒绝；同 IP 的 Linux + OpenSSL 3.x 却能过），
`curl` 与 python 都会失败。因此抓取是"python → 系统 curl"两级兜底，错误统一翻译成人话
（TLS 被重置 / 证书不信任 / DNS / 超时），面板降级为琥珀色并附原因；
可用 `PROXY`（也认 `HTTPS_PROXY`）或换 `STATUS_URL`。

## 执行位与换行符

从 Windows 直接 `git add` 会把脚本记成 644（别人 clone 后 `./install.sh` 会失败）：

```bash
chmod +x install.sh dist.sh uninstall.sh bin/* tests/*.sh    # 在 WSL / macOS 里提交
```

CI 与发布流程一律用 `bash <script>` 调用，不依赖执行位；`.gitattributes` 强制 LF。

## 已知限制

- 会话解析依赖远端有 `zstd` 或 dsh 自带的 node；两边都没有时只显示会话元数据（文件名、时间、大小）。
- macOS 的用户级 launchd 代理没有写 `/etc/hosts` 的权限，别名需要 `dsh-route host-alias` 授权一次
  （或后续支持 `--system` 的 LaunchDaemon）。
- Windows 的 hosts 只能通过一次 UAC 写入，且之后永久有效（Windows 不会重建该文件）。
- 被墙/受限网络下状态页可能取不到，面板会降级但不影响转发功能。
