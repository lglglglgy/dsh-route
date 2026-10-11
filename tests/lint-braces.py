#!/usr/bin/env python3
"""检查（并可修复）"$VAR 紧邻非 ASCII 字符"的写法。

为什么需要它：macOS 自带 bash 3.2 在 UTF-8 locale 下会把多字节字符的首字节
当成变量名的一部分，于是 `$PREFIX（版本…）` 被解析成变量 `PREFIX<字节>`，
配合 set -u 直接 `unbound variable`，安装直接失败（用户终端默认就是 UTF-8 locale）。
写成 `${PREFIX}（版本…）` 没有歧义，所有 bash 版本都安全。

  python3 tests/lint-braces.py .          只检查（CI 用；有问题退出码 1）
  python3 tests/lint-braces.py . --fix    就地修复
"""
import re
import sys
from pathlib import Path

PAT = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7F])")
SKIP_DIRS = {".git", "dist", "build", "__pycache__"}
EXTS = {".sh", ".in", ""}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    fix = "--fix" in sys.argv
    root = Path(args[0] if args else ".")
    hits = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix not in EXTS and path.name != "Makefile":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        found = PAT.findall(text)
        if not found:
            continue
        hits += len(found)
        print("  %-28s %d 处: %s" % (path.relative_to(root), len(found),
                                     ", ".join(sorted(set(found)))))
        if fix:
            path.write_text(PAT.sub(lambda m: "${%s}" % m.group(1), text), encoding="utf-8")
    if fix:
        print("  已修复 %d 处" % hits)
        return 0
    print("  $VAR 紧邻非 ASCII：%d 处（应为 0）" % hits)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
