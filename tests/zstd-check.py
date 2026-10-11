#!/usr/bin/env python3
"""回归测试：dsh 的会话文件是**多帧** zstd 追加写的，必须完整解开。

坑的来源：node 的 `zstdDecompressSync(buf)` 和 python 的 `ZstdDecompressor().decompress(buf)`
都只解第一帧 —— 实测 21200 字节的文件里 26 帧，只解第一帧得到 185 字节，全解是 55563 字节。
所以这里造一个多帧样本，验证 local_zstd_decompress 拿到的字节数等于各帧之和。

  python3 tests/zstd-check.py       没有可用的压缩后端时跳过（退出码 0）
"""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import daemon  # noqa: E402

PARTS = [b'{"type":"x","i":%d}\n' % i * 1 for i in range(60)] + [b'{"type":"y"}\n' * 40]
WANT = sum(len(p) for p in PARTS)

NODE_COMPRESS = ('const z=require("zlib");let d=[];'
                 'process.stdin.on("data",c=>d.push(c))'
                 '.on("end",()=>process.stdout.write(z.zstdCompressSync(Buffer.concat(d))))')


def compress(part: bytes) -> bytes | None:
    if daemon._zstd is not None:
        try:
            return daemon._zstd.compress(part)
        except Exception:
            pass
    if daemon._zstandard is not None:
        try:
            return daemon._zstandard.ZstdCompressor().compress(part)
        except Exception:
            pass
    exe = shutil.which("zstd")
    if exe:
        proc = subprocess.run([exe, "-cq"], input=part, capture_output=True)
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    exe = shutil.which("node")
    if exe:
        proc = subprocess.run([exe, "-e", NODE_COMPRESS], input=part, capture_output=True)
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    return None


frames = [compress(p) for p in PARTS]
if any(f is None for f in frames):
    print("  跳过：本机没有可用的 zstd 压缩后端（python compression.zstd / zstandard / zstd / node）")
    raise SystemExit(0)

blob = b"".join(frames)
print("  多帧样本: %d 帧，共 %d 字节压缩数据，期望解出 %d 字节" % (len(frames), len(blob), WANT))

out = daemon.local_zstd_decompress(blob)
n = len(out) if out else 0
print("  本机后端 %s 解出 %d 字节" % (daemon.zstd_backend(), n))

# 只解第一帧对照（说明这个测试确实能抓住 bug）
one = 0
try:
    if daemon._zstd is not None:
        one = len(daemon._zstd.ZstdDecompressor().decompress(blob))
    elif daemon._zstandard is not None:
        one = len(daemon._zstandard.ZstdDecompressor().decompress(blob))
except Exception:
    one = -1
if one:
    print("  对照（只解第一帧）: %d 字节" % one)

if n != WANT:
    print("  ✗ 不完整：期望 %d 字节，得到 %d（多帧没解全）" % (WANT, n))
    raise SystemExit(1)
if one == WANT:
    print("  ! 注意：本机后端本来就支持多帧，这个测试没覆盖到差异")
print("  ✓ 多帧完整解出")
