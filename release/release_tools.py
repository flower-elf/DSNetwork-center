#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布资产工具（issue #28）。四个子命令只用标准库，发布工作流无需第三方 action：

    files                      打印便携包受管文件清单
    portable                   构建便携 ZIP（附 release-manifest.json）
    checksums                  对最终资产写 checksums.txt
    body                      生成 Release 的下载说明区块（固定文案）

便携 ZIP 按 ``release/portable.txt`` 清单构建，不做全仓通配：这份清单也是
#29 的自更新契约（只替换这些文件、只删除这些过期受管文件），检出目录里误加的
文件不可能混进发布包。

    python release/release_tools.py portable --version 0.2.0 --tag v0.2.0 \
        --out dist/wb-proxy-center-v0.2.0.zip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile

import portable_runtime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MANIFEST = os.path.join(HERE, "portable.txt")
MANIFEST_NAME = "release-manifest.json"
MARKER = "<!-- release-download-block -->"
SCHEMA = 1

# 所有打包文件的根目录，与启动脚本的布局一致：解压得到单一目录，不会散落
# 一堆文件；#29 的自更新也有单一前缀可剥离（写在 manifest 里）。
PACKAGE_ROOT = "wb-proxy-center"

# 自更新永远不能碰的路径。它们随包内 manifest 一起发布，#29 直接按契约断言，
# 不需要第二份硬编码。
PROTECTED = ["accounts/", "usage/"]

# 便携 ZIP 各条目共用的可复现构建时间戳。
DEFAULT_EPOCH = 1700000000


def read_manifest(path=MANIFEST):
    """清单里的路径，按文件顺序，注释行与空行忽略。"""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def source_version(source=ROOT):
    """代码自称的版本号，取 app/wb_proxy.py 的 VERSION。"""
    import re
    with open(os.path.join(source, "app", "wb_proxy.py"), encoding="utf-8") as fh:
        text = fh.read()
    server = re.search(r'(?m)^VERSION\s*=\s*"([^"]+)"', text)
    if not server:
        raise SystemExit("app/wb_proxy.py 里找不到 VERSION")
    return server.group(1)


def _zip_date(epoch):
    import time
    t = time.gmtime(max(epoch, 315532800))  # 1980-01-01 是 ZIP 时间戳下限
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def is_dot_path(name):
    """任一路径组成部分以点开头时为 True。

    发布 ZIP 只装产品文件，.github、.gitignore 这类工具配置无论来自清单还是
    运行时树都不能混进来。
    """
    return any(part.startswith(".") for part in
               str(name or "").replace("\\", "/").split("/") if part)


def build_portable(version, tag, out_path, source=ROOT, runtime_dir=None,
                   root=PACKAGE_ROOT, epoch=DEFAULT_EPOCH):
    """写出便携 ZIP，返回 (path, [受管文件])。

    runtime_dir 是启动脚本在 python/ 下找的那份裁剪 CPython，必填：便携 ZIP
    代替的是绿色包，没有运行时的 ZIP 是另一种、用处小得多的资产。
    """
    got = source_version(source)
    if got != version:
        raise SystemExit("版本不一致：wb_proxy.py=%s，参数=%s" % (got, version))
    if not runtime_dir or not os.path.isdir(runtime_dir):
        raise SystemExit("缺少 --runtime：便携包必须带 python/ 运行时"
                         "（先用 release/portable_runtime.py prepare 生成）")
    portable_runtime.verify(runtime_dir)

    files = read_manifest()
    hidden = [p for p in files if is_dot_path(p)]
    if hidden:
        raise SystemExit("清单里不能有以点开头的路径：%s" % ", ".join(hidden))
    missing = [p for p in files if not os.path.isfile(os.path.join(source, p))]
    if missing:
        raise SystemExit("清单里的文件不存在：%s" % ", ".join(missing))

    entries = []
    crlf_bytes = bytes([13, 10])
    lf_bytes = bytes([10])
    for name in files:
        with open(os.path.join(source, name), "rb") as fh:
            data = fh.read()
        lower = name.lower()
        if lower.endswith((".sh", ".command")):
            # 强制 LF 并赋执行位：Windows 检出的 CRLF 会让 macOS / Linux 启动
            # 报 bad interpreter
            data = data.replace(crlf_bytes, lf_bytes)
            mode = 0o755
        elif lower.endswith(".bat"):
            # Windows 批处理脚本保持 CRLF
            data = data.replace(crlf_bytes, lf_bytes).replace(lf_bytes, crlf_bytes)
            mode = 0o755
        elif lower.endswith((".py", ".json", ".md", ".html", ".txt", "license")):
            data = data.replace(crlf_bytes, lf_bytes)
            mode = 0o644
        else:
            mode = 0o644
        entries.append(("%s/%s" % (root, name), data, mode))
    runtime_files = 0
    for dirpath, _dirnames, filenames in os.walk(runtime_dir):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, runtime_dir).replace(os.sep, "/")
            if is_dot_path(rel):
                continue
            with open(full, "rb") as fh:
                entries.append(("%s/python/%s" % (root, rel), fh.read(), 0o755))
            runtime_files += 1

    manifest = {
        "schema": SCHEMA,
        "name": "workbuddy2api-hub",
        "version": version,
        "tag": tag,
        # files 之下的一切都在这个目录里，自更新剥前缀即可，无需猜压缩包形状。
        "root": root,
        "files": files,
        # 运行时整体算一个受管子树，不占几百行清单。
        "runtime": {
            "prefix": "python/",
            "python": portable_runtime.RUNTIME_VERSION,
            "source": portable_runtime.RUNTIME_URL,
            "files": runtime_files,
        },
        "protected": PROTECTED,
    }
    blob = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    when = _zip_date(epoch)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 排序后相同的输入产出逐字节一致的压缩包。
        for name, data, mode in sorted(entries, key=lambda e: e[0]):
            info = zipfile.ZipInfo(name, date_time=when)
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix，上面的 mode 才能保留
            zf.writestr(info, data)
        info = zipfile.ZipInfo(MANIFEST_NAME, date_time=when)
        info.external_attr = 0o644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        zf.writestr(info, blob.encode("utf-8"))
    return out_path, files


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(assets, out_path):
    """对最终上传的资产按文件名排序，写 sha256sum 格式的校验值。"""
    lines = []
    for path in sorted(assets, key=lambda p: os.path.basename(p)):
        lines.append("%s  %s\n" % (sha256(path), os.path.basename(path)))
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(lines)
    return lines


def download_block(zip_name):
    """issue #28 固定的下载说明区块，保持简短与稳定。"""
    return "\n".join([
        "## 下载说明",
        "",
        "- `%s`" % zip_name,
        "  - 绿色包（内置 Windows Python 运行时 + 看板 + 启动脚本）",
        "  - 解压后运行 start-wb-proxy.bat",
        "  - macOS / Linux 使用系统 Python 3.9+ 运行对应启动脚本",
        "",
        "- `checksums.txt`",
        "  - Release 资产的 SHA-256 校验值",
        "",
    ])


def compose_body(existing, block):
    """marker 之上保留维护者写的全部内容，其后接新下载区块。

    重复执行是幂等的：第二遍在 marker 上方取到同样的文本，资产列表更新既不会
    重复出块，也不会吃掉维护者的笔记。
    """
    above = ""
    if existing:
        above = existing.split(MARKER, 1)[0].rstrip()
    if not above:
        above = "<!-- 版本说明写在这里；下面的「下载说明」由 release 工作流维护 -->"
    return above + "\n\n" + MARKER + "\n" + block


def asset_names(version):
    """便携构建为一个发布产出的唯一资产名。"""
    return "wb-proxy-center-v%s.zip" % version


def _utf8_stdio():
    """把标准输出与标准错误固定为 UTF-8，让进度行在非 UTF-8 控制台下也能打印。

    Windows runner 给 Python 的 stdout 是 cp1252，第一条中文摘要就会抛
    UnicodeEncodeError。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("files", help="print the managed portable file list")

    p = sub.add_parser("portable", help="build the portable ZIP")
    p.add_argument("--version", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--source", default=ROOT)
    p.add_argument("--runtime", required=True,
                   help="trimmed CPython tree (release/portable_runtime.py prepare)")
    p.add_argument("--root", default=PACKAGE_ROOT,
                   help="folder every packaged file lives under (default %s)" % PACKAGE_ROOT)
    p.add_argument("--epoch", type=int, default=int(os.environ.get("SOURCE_DATE_EPOCH", DEFAULT_EPOCH)))

    c = sub.add_parser("checksums", help="write checksums.txt")
    c.add_argument("--out", required=True)
    c.add_argument("assets", nargs="+")

    b = sub.add_parser("body", help="compose the Release download section")
    b.add_argument("--version", required=True)
    b.add_argument("--existing", default=None, help="file with the current body ('-' for stdin)")
    b.add_argument("--out", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "files":
        for name in read_manifest():
            print(name)
        return 0

    if args.cmd == "portable":
        path, files = build_portable(args.version, args.tag, args.out,
                                     source=args.source, runtime_dir=args.runtime,
                                     root=args.root, epoch=args.epoch)
        print("portable: %s（%d 个受管文件 + python/ 运行时 %s + %s）"
              % (path, len(files), portable_runtime.RUNTIME_VERSION, MANIFEST_NAME))
        print("sha256  : %s" % sha256(path))
        return 0

    if args.cmd == "checksums":
        lines = write_checksums(args.assets, args.out)
        sys.stdout.writelines(lines)
        return 0

    if args.cmd == "body":
        existing = ""
        if args.existing == "-":
            existing = sys.stdin.read()
        elif args.existing and os.path.isfile(args.existing):
            with open(args.existing, encoding="utf-8") as fh:
                existing = fh.read()
        zip_name = asset_names(args.version)
        text = compose_body(existing, download_block(zip_name))
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("body    : %s（%d 字节）" % (args.out, len(text.encode("utf-8"))))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
