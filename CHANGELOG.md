# 变更记录

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

