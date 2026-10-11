# 开发用快捷入口（不是必须，脚本本身都能直接跑）
VERSION := $(shell tr -d '[:space:]' < VERSION)

.PHONY: dist test check install clean version

version:           ## 当前版本
	@echo $(VERSION)

dist:              ## 构建 dist/dsh-route-<version>.run 与 .tar.gz
	./dist.sh

test:              ## 平台分支测试（macOS 发现桩 / launchd plist / 系统 unit / 无 systemd 兜底 / 清单卸载）
	bash tests/platform-test.sh

check: dist        ## 产物自检：拿构建出来的 .run 真装真卸（全隔离）
	bash tests/release-check.sh "dist/dsh-route-$(VERSION).run"

smoke:             ## 冒烟测试（需要有正在运行的服务）
	bash tests/smoke.sh

install:           ## 安装/升级到当前用户
	./install.sh

clean:
	rm -rf dist build
