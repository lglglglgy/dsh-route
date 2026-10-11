# dsh-route

[![CI](https://github.com/lglglglgy/dsh-route/actions/workflows/ci.yml/badge.svg)](https://github.com/lglglglgy/dsh-route/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/lglglglgy/dsh-route)](https://github.com/lglglglgy/dsh-route/releases/latest)

在 WSL / Linux / macOS 上常驻运行的**远端 dsh web 路由台**：自动发现你当前 ssh 连着的机器，验证它们上面的 dsh web 端口，
为每个验证通过的端口建立并保活一条本地转发，最后给一个页面：左边是当前连接，右边是这些机器的最近会话
（token 用量 / 是否在跑 / 是否卡在审批）＋ DeepSeek 余额、余额走势、近期使用、API 状态。

> 一句话安装（下载发布页里的 `.run` 后）：`./dsh-route-0.1.0.run`

```
宿主机/ WSL 里 ssh ├── h100      3080,3081,3082 ──┐
                   └── hwdocker  3080            ─┤  逐个验证（401 + "dsh web authentication required"）
                                                  ├─► 本地 127.0.0.1:3080 / 3081 / 3082 / 3083
控制页 http://127.0.0.1:3090/  ◄──────────────────┘
```

## 支持平台

| 平台 | 本机 ssh 发现 | 服务托管 | 说明 |
| --- | --- | --- | --- |
| **Linux** | `/proc/<pid>` | `systemd --user`（默认）／`systemd` 系统级（`--system`，需 sudo）／无 systemd 时兜底后台进程 + `crontab @reboot` | 服务器上建议 `loginctl enable-linger $USER`，否则注销后 user 服务会停 |
| **WSL** | `/proc` +（额外）Windows `ssh.exe` 命令行 + 网络对端 | 同 Linux（`systemd --user`） | 宿主机侧的 ssh 目标也能发现；需要 `/etc/wsl.conf` 里 `systemd=true` |
| **macOS** | `ps -Ao comm=,args=` | `launchd` 用户代理（`~/Library/LaunchAgents/com.dshroute.agent.plist`） | 无需 sudo；日志在 `~/Library/Logs/dsh-route.log` |

安装脚本自动选服务类型，也可以强制：`--service-kind auto|systemd|systemd-system|launchd|none`、`--system`。

## 特点

- **只认验证过的 dsh web**：候选端口（默认 3080/3081/3082）逐个探测，必须返回
  `401 + "dsh web authentication required"` 才建立隧道；别的 HTTP 服务会被标成"非 dsh，未转发"。
- **自动发现，不依赖 ssh config**：宿主机侧读 Windows 的 `ssh.exe` 命令行，WSL 侧读 `/proc`；
  两边都扫，连上就出现、断开就回收。
- **每个端口各自保活**：一个"主机:远端端口"一条隧道，掉线按 2→30 秒退避重连。
- **有内容的页面**：最近会话（取自各机器 `DSH_HOME/sessions`，解析 token 与运行/阻塞状态）、
  DeepSeek 余额与走势、status.deepseek.com 的组件状态与事件、近 7 天会话柱状图。
- **零 root**：全部装在用户目录，systemd --user 托管；代码/配置/状态分离，升级只覆盖代码。

## 依赖

| 依赖 | 用途 | 缺失时 |
| --- | --- | --- |
| python3 ≥ 3.9 | 守护进程 | 必需 |
| 会话解析：**远端**的 `zstd` / **dsh 自带的 node**（逐帧解）/ 带 zstd 的 python 任一 | 解开 `session.v4.jsonl.zstd`，读出 token 与运行状态 | 会话面板只显示元数据 |
| 会话解析：本机的 `compression.zstd`(3.14+)/`zstandard`/`zstd`/`node` 任一 | 远端都解不开时在本机兜底 | 同上（**不要求本机装 3.14**） |
| curl / ssh | 探测、隧道 | 必需（macOS 自带） |
| 服务管理器：`systemd --user` / `launchd` | 常驻与自启 | 自动退化为后台进程 + `crontab @reboot` |
| WSL：Windows PowerShell | 发现宿主机侧的 ssh 目标 | 宿主目标发现不到（WSL 内 ssh 仍可） |
| WSL：`/etc/wsl.conf` 里 `systemd=true` | `systemctl --user` | 退化为后台进程 |

**会话解析是两级兜底**：优先让**远端**解压（远端跑着 dsh，所以 dsh 自带的 node 必然在；
`zstd` 命令和带 zstd 的 python 也认），远端都不行才取原始文件在**本机**解
（`compression.zstd` → `zstandard` → `zstd` 命令 → `node`）。所以本机不需要 Python 3.14；
想在本机兜底又不想升级解释器，`pip install zstandard` 或 `brew/apt install zstd` 任选其一即可。
当前走的是哪条路，`dsh-route status` 的 `probe.zstd` 以及每个会话的 `via` 字段都会显示。

> ⚠️ dsh 的会话文件是**多帧** zstd（一个 21KB 文件里 26 帧）。node 的 `zstdDecompressSync(buf)` 和
> python 的 `ZstdDecompressor().decompress(buf)` **只解第一帧**（55KB 的会话只得到 185 字节，
> 表现为"会话在但 token 全是 0"）。本项目按帧头魔数切分后逐帧解，并用"结果是否以换行结尾"做完整性校验，
> 必要时换另一侧再解一遍取更长的。`tests/zstd-check.py` 专门守这个回归。

**找 dsh web 的 token 也不依赖 tmux**：顺序是 ① 运行中的 dsh web 进程的命令行与环境变量（`/proc`）→
② 各类日志文件（dsh web 的启动输出里带 URL）→ ③ tmux 面板（装了就顺手捞，没装也照样找得到）→
④ `ps` 兜底。找到的候选还会真的 curl 一遍（只有返回 303 的才算数），所以不会拿错 token。

## 安装

### 方式零：从 GitHub 发布页下载（推荐）

```bash
curl -fsSLO https://github.com/lglglglgy/dsh-route/releases/latest/download/dsh-route-0.1.0.run
chmod +x dsh-route-0.1.0.run
./dsh-route-0.1.0.run
```

### 方式一：单文件自解压 `.run`（推荐）

和 CUDA 的 `.run` 一个路子：一个文件，`chmod +x` 后直接跑。

```bash
./dsh-route-0.1.0.run                 # 安装（或升级）并启动
```

常用选项（桩自己解析，其余参数原样交给包内 `install.sh`）：

```bash
./dsh-route-0.1.0.run --check          # 只做依赖体检，不改系统
./dsh-route-0.1.0.run --extract=DIR    # 只解包到 DIR
./dsh-route-0.1.0.run --list           # 列出包内容
./dsh-route-0.1.0.run --noexec --keep  # 解包但不执行安装
./dsh-route-0.1.0.run --target DIR     # 指定解包临时目录（默认临时目录，用后删除）
./dsh-route-0.1.0.run --version

./dsh-route-0.1.0.run --prefix ~/opt/dsh-route --bin-dir ~/.local/bin
./dsh-route-0.1.0.run --no-service
./dsh-route-0.1.0.run --uninstall [--purge]
```

### 方式二：普通压缩包

```bash
tar xzf dsh-route-0.1.0.tar.gz
cd dsh-route-0.1.0 && ./install.sh
```

### `.run` 是怎么实现的

文件本身就是个 shell 脚本，结构是 **shell 桩 + 标记行 + tar.gz 载荷**：

```
#!/bin/sh
… 桩：解析参数、切载荷、校验、解包、调用 install.sh …
__DSH_ROUTE_PAYLOAD_BELOW__      ← 标记行
<tar.gz 二进制载荷>
```

运行流程：

1. `awk` 找到标记行的行号 `N`；
2. `tail -n +N "$0" > payload.tar.gz` —— `tail` 是按字节复制剩余内容，所以拼接二进制安全；
   （打包时就把 `N`、版本、载荷 sha256 写死在桩里了）
3. `sha256sum` 比对内置哈希，不一致直接报错退出（传输损坏拦在这里）；
4. 解到临时目录，执行里面的 `install.sh`（所有安装逻辑仍在 `install.sh`，`.run` 只是外壳）；
5. 除非 `--keep`/`--target`，否则删掉临时目录。

想自己重打：`bash dist.sh` 会同时产出 `.run`、`.tar.gz` 和各自 `.sha256`。

安装脚本是**幂等**的：升级就是解包后再跑一次 `./install.sh`。它会

1. 检查依赖（python/curl/ssh/systemd/WSL/PowerShell）并给出可执行的提示；
2. 把 `daemon.py`、`ui.html`、5 个字体（本地提供，不连外网）装进代码目录；
3. **只在不存在时**生成 `~/.config/dsh-route/config`；已存在则不动，只提示新增键；
4. 渲染并安装 systemd user unit（带 `DSH_ROUTE_PREFIX/CONFIG/KEY_FILE`），`enable --now`；
5. 等控制页 200 后打印地址与当前在线目标数，失败时贴日志尾部。

## 使用

```bash
dsh-route                  # 当前连接与转发状态
dsh-route url h100         # 打印某台可直接打开的 URL（带 token）
dsh-route open             # 控制页地址
dsh-route key              # 录入 DeepSeek API key（余额面板用，不回显）
dsh-route where            # 代码/配置路径、端口配置、服务状态
dsh-route restart|log      # 服务控制
```

浏览器打开 **http://127.0.0.1:3090/** —— 请用 `127.0.0.1` 而非 `localhost`：dsh web 的信任栅栏按
Host 判定。页面上：左栏是当前连接的机器与转发端口，右栏是该机器最近会话；下面是余额、余额走势、
近期使用、API 状态。右上角齿轮可录入 API key。

## 卸载

安装时会把**安装清单**写到 `~/.local/share/dsh-route/MANIFEST`（记录所有实际路径），
并把卸载器放进 PATH，所以卸载**不需要保留发行包**：

```bash
dsh-route uninstall            # 停服务、删代码/客户端/服务定义/运行日志；保留配置与余额历史
dsh-route uninstall --purge    # 连配置、API key、余额历史一起删
dsh-route uninstall --dry-run  # 只打印会删什么
```

`~/.local/bin/dsh-route-uninstall` 也可以直接调用。卸载器只删清单里登记过的东西，
遇到 `/`、`$HOME` 或未登记的路径会拒绝执行，最后会做一次残留自检。

如果代码目录已被手工删掉、清单不可用，它会退回"按默认位置 + 平台探测"清理并明确提示。

> 发行包（`.run`/`.tar.gz`）里也带 `./install.sh --uninstall`，效果相同——这条只是给"还没装"或"清单没了"的情况用。

## 目录与状态

| 位置 | 内容 |
| --- | --- |
| `~/.local/share/dsh-route/` | 代码：`daemon.py`、`ui.html`、`assets/*.woff2`、`VERSION`、`MANIFEST`（安装清单） |
| `~/bin/dsh-route`、`~/bin/dsh-route-uninstall` | 命令行客户端与卸载器（`dsh-route uninstall`） |
| `~/.config/dsh-route/config` | 配置（安装时生成，升级不动） |
| `~/.config/dsh-route/deepseek.key` | API key（权限 600） |
| `~/.local/state/dsh-route/balance-history.json` | 余额采样历史（走势图） |
| `$XDG_RUNTIME_DIR/dsh-route/daemon.log` | 运行日志（macOS/无 XDG 时落到 `/tmp/dsh-route/`） |
| Linux/WSL：`~/.config/systemd/user/dsh-route.service` | systemd user unit |
| Linux 系统级：`/etc/systemd/system/dsh-route.service` | `--system` 时使用 |
| macOS：`~/Library/LaunchAgents/com.dshroute.agent.plist` | launchd 代理；日志 `~/Library/Logs/dsh-route.log` |
| 无服务管理器：`~/.local/share/dsh-route/run.sh` + `~/.local/state/dsh-route/daemon.log` | 兜底启动脚本（crontab `@reboot` 自启） |

## 配置要点

```ini
BASE_PORT=3080                 # 本地端口起点
REMOTE_PORTS=3080,3081,3082    # 远端候选端口（逐个验证）
CONTROL_PORT=3090              # 控制页，只监听 127.0.0.1
POLL=5 / METRICS_POLL=30       # 发现周期 / 指标刷新周期
PROXY=                         # 可选：http://127.0.0.1:7890，状态页/接口直连受限时走代理
```

完整说明见 `config/config.example`。

### 状态页取不到时（例如 macOS 上）

有些网络里 `status.deepseek.com`（Flashcat CDN）会**在 TLS 握手阶段直接重置连接**，
典型是 CDN/WAF 按客户端 TLS 指纹（JA3）拦截：同一台机器上 `curl`（LibreSSL）与 python 都会失败，
而同一出口的 Linux 机器用 OpenSSL 3.x 却正常。这不是服务故障，也不是证书问题（`api.deepseek.com` 通常仍然通）。

面板在这种情况下不会一片红，而是**琥珀色降级显示**：
标题写"状态页取不到（本机网络原因）"，给出 `原因`（如"TLS 被重置"）、`详情`（原始报错），
同时把"本机直连 api.deepseek.com · HTTP 200 · 83 ms"和可用模型列出来当作健康信号。

要在本机看到组件可用率，三条路：

1. 配置 `PROXY=http://127.0.0.1:7890`（也认 `HTTPS_PROXY` 环境变量）；
2. 把 `STATUS_URL` 指到能访问的镜像/反向代理；
3. 换用新 TLS 栈的客户端（Homebrew 的 `curl`/`python`，OpenSSL 3.x）——本程序会自动改用它。

## 测试

```bash
bash tests/release-check.sh dist/dsh-route-0.1.0.run   # 产物自检：拿构建出来的 .run 真装真卸（全隔离）
bash tests/platform-test.sh    # 平台分支：macOS ps 发现（桩）、launchd plist、系统 unit、无 systemd 兜底、清单卸载
./tests/smoke.sh            # 冒烟：服务/页面/字体/路径穿越/API 结构/每个转发端口（需要在跑的服务）
```

`make test` / `make check` / `make smoke` 是同样的入口。

## 仓库结构与开发

```
install.sh            安装/升级/卸载入口（幂等；卸载优先交给下面的卸载器）
dist.sh               构建 dist/*.run 与 *.tar.gz（.run = shell 桩 + tar.gz 载荷）
bin/dsh-route         命令行客户端（status/url/key/where/start/uninstall…）
bin/dsh-route-uninstall  卸载器（安装时被放进 PATH，读 MANIFEST 精确删除）
src/daemon.py         守护进程（发现/探测/隧道/指标/HTTP 服务）
src/ui.html           控制页（单文件，含粒子标题与动态背景）
service/              systemd user / systemd 系统 / launchd 三套服务定义模板
selfextract/stub.sh.in  .run 的自解压桩模板
config/config.example 配置模板（安装时不覆盖已有配置）
tests/                产物自检 / 平台分支测试 / 冒烟测试
.github/workflows/    CI 与自动发布
```

本地开发：直接改 `src/`、`service/`、`bin/`，然后

```bash
make test      # 快，不需要跑服务
make check     # 构建 + 用产物真装真卸
bash install.sh   # 装到本机（会覆盖代码，不动配置与历史）
```

## 发布流程（维护者）

```bash
# 1) 改版本与变更记录
$EDITOR VERSION CHANGELOG.md
# 2) 提交并打 tag（tag 必须与 VERSION 一致，CI 会校验）
git add -A && git commit -m "release: v0.1.1"
git tag v0.1.1 && git push origin main --tags
```

推 tag 后 `.github/workflows/release.yml` 会自动：

1. 校验 `tag == VERSION`；
2. `bash dist.sh` 构建 `.run` 与 `.tar.gz`；
3. 跑 `tests/release-check.sh`（把 `.run` 解包→沙箱安装→卸载→purge，全隔离）与 `sha256sum -c` 自检；
4. 建 GitHub Release，附 `.run`、`.tar.gz` 及两个 `.sha256`，正文带下载表与安装命令。

`.github/workflows/ci.yml` 在 push 到 main / PR 时跑：Linux 与 macOS 两边做语法检查、shellcheck（只拦 error 级）、
构建、产物自检与平台分支测试。

首次上传：

```bash
git init && git add -A && git commit -m "dsh-route 0.1.0"
git branch -M main
git remote add origin git@github.com:lglglglgy/dsh-route.git
git push -u origin main
```

> **执行位**：从 Windows 直接 `git add` 会把脚本记成 644（别人 clone 后没有执行位，`./install.sh` 会失败）。
> 在 WSL / macOS 里先 `chmod +x install.sh dist.sh uninstall.sh bin/* tests/*.sh` 再提交即可
> （这样 `git ls-files -s` 里会是 100755）。CI 与发布流程一律用 `bash <script>`，不依赖执行位。

本地开发同理：用 `bash install.sh`、`bash dist.sh` 就不挑执行位。

## 排障

- **列表空的**：先确认你真的 `ssh` 到了目标机（这是唯一发现口径）；`dsh-route where` 会打印
  平台、服务类型与发现方式（Linux/WSL 用 `/proc`，macOS 用 `ps`）。
- **某台显示"非 dsh"**：那个端口上的服务没有返回 dsh 的特征响应，按设计不转发。
- **页面显示"无 token"**：远端 `dsh web` 的启动输出里没有抓到 token（不在 tmux / 没写日志），
  可以用 `dsh-route url <host>` 或手动带上 token 打开。
- **余额面板空**：`dsh-route key` 录入 key（或写进 `~/.config/dsh-route/deepseek.key`）。
- **macOS 服务没起**：`launchctl print gui/$(id -u)/com.dshroute.agent`，日志在 `~/Library/Logs/dsh-route.log`；
  重装服务用 `dsh-route restart`。
- **Linux 服务器重启后没起**：默认装的是 user 服务，需要 `loginctl enable-linger $USER`；
  或改用系统级：`./install.sh --system`。
- **完全不想用服务管理器**：`./install.sh --service-kind none`，会写成 `run.sh` 后台进程 + `crontab @reboot`。

## 许可

MIT
