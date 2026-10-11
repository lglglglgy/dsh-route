#!/usr/bin/env bash
# 卸载入口（等价于 ./install.sh --uninstall）
#   ./uninstall.sh            卸载代码与 unit，保留配置 / 余额历史
#   ./uninstall.sh --purge    连配置、余额历史一起删除
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" --uninstall "$@"
