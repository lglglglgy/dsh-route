# 变更记录

## 0.1.6

- README：快速开始不再硬编码版本号（自动取最新 tag），并说明安装时会提示一次管理员密码 / UAC。
- 测试： 参数透传断言改为检查输出内容（不再依赖 dry-run 的退出码，避免误报）。

## 0.1.5

- **安装会自动写访问别名**
  - WSL：安装时自动弹一次 UAC，把 `dsh.local` 写进 **Windows 的 hosts**（浏览器在 Windows 侧，这份才长效）；
    已有则跳过，不重复弹。
  - macOS / Linux：能直接写就写；否则**图形授权优先**——本地终端里跑弹系统授权框（可按指纹），
    非交互（脚本 / ssh）自动打开一个授权窗口；都没有才退回终端密码 / 打印命令。
- **卸载会真正清理别名**：按 `# dsh-route` 标记清 `/etc/hosts`（可写→免密 sudo→图形授权→授权窗口→终端 sudo），
  WSL 下同时用 UAC 清 Windows hosts；实在没权限会明说还剩什么、怎么清。
- **`.run` 参数透传修复（重要）**：自解压桩原用白名单校验参数，`--host-alias` / `--no-host-alias` /
  `--windows-hosts` / `--system` / `--purge` 等文档化参数会被拒（"未知参数"）；现在全部原样转给 install.sh，
  并加了回归断言。
- **测试与真机隔离**：沙箱/CI 的安装卸载不再停用正在运行的服务（比对 systemd 实际 unit）、不再触碰真实
  Windows hosts；安装侧也只在"用户正常安装"（家目录下、非测试环境）时才弹 UAC。
- 安装结束做**别名自检**：明确报告 `http://dsh.local:3090/ ✓ 可用`，或说明为什么没成。

## 0.1.4

- **访问别名三层自动**：
  - 安装时：交互式跑 `.run` 会提示输入一次密码写 `/etc/hosts`；WSL 里还会**自动弹一次 UAC**
    把别名写进 **Windows 的 hosts**（`--windows-hosts` 强制、`--no-windows-hosts` 跳过、
    `DSH_ROUTE_NO_PROMPT=1` 禁止提问）；非交互场景只打印命令，不会卡住。
  - 运行时：守护进程**启动时 + 每 5 分钟**检查别名在不在，不在就补回来（`ensure_host_alias`）。
    装成系统服务（`./install.sh --system`，root）时全自动——WSL 每次启动重建 `/etc/hosts`
    也会被服务补回；用户级服务没权限时只在 `status` 与页面上提示原因。
  - 手动：`dsh-route host-alias [NAME]`、`--windows`（弹 UAC 写 Windows）、`--remove` / `--windows-remove`。
  - 卸载：`/etc/hosts` 与 **Windows hosts** 里带 `# dsh-route` 标记的行都会清掉（后者同样一次 UAC）。
- 文档补充：从 Windows 浏览器访问 WSL 服务时 `localhost` 会被解析成 `::1`（WSL 转发是 IPv4），
  请用 `127.0.0.1:3090` 或 `dsh.local:3090`。

## 0.1.3

- **访问地址不再是"127.0.0.1:3090"这一种**：
  - 控制页**同时监听 127.0.0.1 与 ::1**，所以 `http://localhost:3090/` 在把 localhost 解析成 IPv6 的
    系统上也能开（以前只监听 IPv4 时可能打不开）；
  - 新增访问别名 `HOST_ALIAS`（默认 `dsh.local`）：安装时写进 `/etc/hosts`（带 `# dsh-route` 标记，
    **卸载时自动移除**），之后用 `http://dsh.local:3090/` 打开；没有免密 sudo 时会打印一行命令，
    也可以随时跑 `dsh-route host-alias`（会提示输密码），`dsh-route host-alias --remove` 移除；
  - `dsh-route status / open / where` 与页面底部都显示当前可用的访问地址，并提示别名是否已生效。
  - WSL 提示：WSL 的 `/etc/hosts` 每次启动会重建，建议把别名加进 Windows 的 hosts 文件
    （`C:\Windows\System32\drivers\etc\hosts`），这样 Windows 浏览器也能直接访问。
- **标签页图标**：新增自带矢量图标（渐变圆角方块 + "多路汇入"示意）`favicon.svg` / `favicon-32.png` /
  `favicon.ico` 和 `apple-touch-icon.png`（固定标签页/加桌面），并给 `/favicon.ico` 与
  `/apple-touch-icon.png` 加了路由——不再显示浏览器默认的地球；安装脚本现在会把 `src/assets/` 下
  所有资源都装进去（以前只装 `*.woff2`）。

## 0.1.2

- **不再要求本机 Python 3.14**：会话解析改成两级兜底。优先让**远端**解压
  （远端 `zstd` 命令 → **dsh 自带的 node**（按帧循环解） → 带 zstd 的 python），
  远端都不行才取原始文件在**本机**解（`compression.zstd` → `zstandard` → `zstd` → `node`）。
  本机不想升级解释器就 `pip install zstandard` 或 `brew/apt install zstd` 任选其一。
- **踩到的坑（已修 + 已加回归测试）**：dsh 的 `session.v4.jsonl.zstd` 是**多帧追加**写的
  （实测一个 21200 字节的文件里有 26 帧）。node 的 `zstdDecompressSync(buf)` 与 python 的
  `ZstdDecompressor().decompress(buf)` 都**只解第一帧**——55KB 的会话只得到 185 字节，
  表现为"会话在但 token/标题全是 0"。现在 node 按帧头魔数 `28 B5 2F FD` 切分后逐帧解，
  再用"结果是否以换行结尾"做完整性校验、必要时换另一侧再解一遍取更长的。
  新增 `tests/zstd-check.py`：造 61 帧样本断言完整解出（只解第一帧会少 98%）。
- **找 dsh web 的 token 不再依赖 tmux**：① dsh web 进程的命令行与环境变量（`/proc`）
  → ② 各类日志（`~/dsh*.log`、`~/nohup.out`、`$DSH_HOME/**/*.log`、`/tmp/dsh*.log`…）
  → ③ tmux 面板（装了就顺手捞）→ ④ `ps` 兜底；候选 token 仍会 curl 复验（只有 303 才算数）。

## 0.1.1

- **修复 macOS 安装失败**（UTF-8 locale + 自带 bash 3.2）：`$VAR` 紧邻中文/全角字符时，
  bash 3.2 会把多字节字符的首字节当成变量名的一部分，配合 `set -u` 直接报 unbound variable，
  安装在中途中断。全仓库 28 处改为 `${VAR}`，并新增 `tests/lint-braces.py` 在 CI 里防回归。
- 这个 bug 只在 macOS + UTF-8 locale 下出现（用户终端默认如此），Linux 上跑不出来——
  CI 的 macOS job 正是为此存在。

## 0.1.0 — 初版

### 卸载重做：清单驱动 + 卸载器进 PATH

- 安装时写 `$PREFIX/MANIFEST`（记录 prefix、bin、客户端、卸载器、配置、状态、unit/plist、label、
  运行目录、日志、crontab 行、服务类型），并生成**自带本次安装路径**的 `$BIN_DIR/dsh-route-uninstall`。
- 用户只用 `dsh-route uninstall [--purge|--dry-run]`（或直接跑 `dsh-route-uninstall`）：
  读清单精确删除，**不需要保留发行包**；清单不可用时退回"默认位置 + 平台探测"清理并提示。
- 删除前做安全检查：`/`、`$HOME`、未登记路径一律拒绝；结束时做残留自检。
- `--unit-dir` 不再是命令行参数（改成内部环境变量 `DSH_ROUTE_UNIT_DIR`）；沙箱/自定义安装也不会误动线上
  （卸载器只处理自己清单里的路径）。自定义安装可用 `DSH_ROUTE_STATE_DIR` 指定运行态目录，
  unit/plist/run.sh 都会带上，`--purge` 因而不会误删线上余额历史。

### 真机 macOS / 真实网络验证后修掉的问题

1. **macOS 上"发现"永远为空**：`ps -Ao comm=,args=` 的输出里可能夹带非 UTF-8 字节（进程参数），
   `subprocess.run(..., text=True)` 会在解码时抛 `UnicodeDecodeError` 并被吞掉。
   改为按字节收取再 `decode("utf-8", "replace")`；`ssh_run()` 的远端输出同样改掉
   （同类问题之前只在 Windows/PowerShell 侧处理过）。
2. **macOS 没有 `setsid`**：`--service-kind none`（无 systemd/launchd 的兜底）用 `setsid nohup` 起进程，
   在 Darwin 上直接失败。安装脚本与客户端都改为"有 setsid 就用，没有就用子 shell + nohup"。
   另外测试脚本里的 `sed -i` 在 BSD sed 上语义不同，改成不用 `-i` 的就地替换。
3. **状态页在某些网络下抓不到**（`status.deepseek.com` 在 TLS 握手阶段被 CDN/WAF 重置，
   同机器 `curl` 与 python 都被拒、同出口的 Linux 用 OpenSSL 3.x 正常）：
   - 抓取改为 **python(urllib) → 系统 curl** 两级兜底（两者 TLS 栈不同，常常一个能过一个不能），
     `api.deepseek.com` / 余额接口同样支持 curl 兜底；
   - 新增 `PROXY` 配置（也认 `HTTPS_PROXY`），并给 `STATUS_URL` 留了换镜像的入口；
   - 网络异常统一翻译成"人话 + 可操作提示"（TLS 被重置 / 证书不信任 / DNS / 超时），
     面板不再只显示一句"状态页不可达"：改成**琥珀色降级**，附原因、原始报错，
     并用"直连 api.deepseek.com 正常"当健康信号。

### 另一批（卸载 + systemd 日志路径）

- **卸载做成"清干净 + 自检"**：一次 `--uninstall` 会停服务（systemd/launchd/crontab/兜底进程全试一遍）、
  删 unit/plist/crontab 行、删代码目录与客户端、删运行目录与日志（`$XDG_RUNTIME_DIR/dsh-route`、
  `/tmp/dsh-route`、`~/Library/Logs/dsh-route.log`）与安装临时文件；然后逐项检查是否还有残留并明确报告。
  `--purge` 才删配置、key 与余额历史（默认保留）。
- **卸载只影响自己那一份**（本 prefix）：服务定义里不含本 prefix、或指定了 `--unit-dir`（沙箱）时，
  不执行 stop/bootout，也不会删真实默认目录下的 unit/plist。`launchd` 的 Label 是共享的，这条尤其重要。
- **systemd unit 不再用 `StandardOutput=append:%t/...`**：systemd 打开该日志文件比创建目录更早，
  运行目录被删过（例如刚卸载完又重装）就会以 `status=209/STDOUT` 永久启动失败。改为日志走 journal，
  文件日志由守护进程自己写（它会自建目录）。已加回归测试：删掉运行目录后重启仍能起来。

- 自动发现：Linux/WSL 扫 `/proc`，macOS 用 `ps -Ao comm=,args=`（两种 argv 形态都兼容，过滤 sshd/ssh-agent，跳过 `-N -L`）；
  WSL 里额外读 Windows `ssh.exe` 命令行，宿主机侧目标也能发现。
- 多端口验证转发：候选端口逐个探测，必须返回 `401 + "dsh web authentication required"` 才建立隧道；
  每个"主机:远端端口"一条隧道，退避重连保活。
- 服务托管三选一（自动探测，可 `--service-kind` 强制）：`systemd --user`、`systemd` 系统级（`--system`）、
  macOS `launchd` 用户代理、以及无服务管理器时的后台进程 + `crontab @reboot` 兜底。
- 控制页：浅→深渐变动态背景、粒子标题（鼠标经过被打散后缓慢归位并停帧省电）、
  左栏机器/端口列表、右栏最近会话（token 用量、运行中/等待审批/已结束）。
- 路由台：DeepSeek 余额、余额走势（采样落盘，重启保留）、近 7 天使用柱状图、
  status.deepseek.com 组件状态与事件。
- 设置抽屉：页面内录入 DeepSeek API key（写入 `~/.config/dsh-route/deepseek.key`，权限 600）。
- 打包：单文件自解压 `dsh-route-0.1.0.run`（shell 桩 + tar.gz 载荷 + sha256 校验）、普通 tarball、
  `install.sh`（幂等，可 `--dry-run/--uninstall/--purge/--prefix/--service-kind`）、`dist.sh`。
- 测试：`tests/smoke.sh`（服务/页面/字体/路径穿越/API/每个转发端口）、
  `tests/platform-test.sh`（macOS ps 发现桩、launchd plist、系统 unit、无 systemd 兜底真启停）。
- 已知边界：会话 token 需要远端 python3 ≥ 3.14 的 `compression.zstd`（本机解析）；无则只显示元数据。

