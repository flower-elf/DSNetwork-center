#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布门禁要等待的测试矩阵腿，每行打印一个任务名（issue #28）。

发布要求目标提交上整个矩阵全部通过。tests 工作流的总结论不够用：一条腿从
矩阵里删掉时它照样是绿的，门禁会被悄悄削弱。所以腿直接解析自
`.github/workflows/tests.yml`，输出形状与 Actions API 上报的任务名一致，
即 `<os> / python <version>`。在那里加一条腿，门禁自动跟进。当前矩阵是
Ubuntu 3.9、Ubuntu 3.12、Windows 3.12 三条腿，以 tests.yml 为准。

    python release/matrix_legs.py

退出码：至少打印一条腿为 0，解析不出腿为 1。门禁点不出腿的名字就必须拒绝。
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TESTS_YML = os.path.join(ROOT, ".github", "workflows", "tests.yml")

# tests.yml 里每个任务名是 `${{ matrix.os }} / python ${{ matrix.python }}`。
JOB_NAME = "%s / python %s"


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_include(text):
    """第一个 `matrix.include` 列表的 [(key, value), ...] 条目。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*include:\s*(#.*)?$", line):
            start = i + 1
            break
    if start is None:
        return []

    entries = []
    current = None
    block_indent = None
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if block_indent is None:
            if not stripped.startswith("-"):
                return []          # include: 后面不是列表
            block_indent = indent
        elif indent < block_indent:
            break                  # 列表结束
        if stripped.startswith("-"):
            current = {}
            entries.append(current)
            stripped = stripped[1:].strip()
        if not stripped or current is None:
            continue
        key, _, value = stripped.partition(":")
        if not _:
            continue
        current[key.strip()] = _unquote(value)
    return entries


def legs(text=None):
    """每条矩阵腿的任务名，按文件顺序。"""
    if text is None:
        with open(TESTS_YML, encoding="utf-8") as fh:
            text = fh.read()
    out = []
    for entry in parse_include(text):
        os_name, python = entry.get("os"), entry.get("python")
        if os_name and python:
            out.append(JOB_NAME % (os_name, python))
    return out


def main():
    names = legs()
    if not names:
        print("无法从 .github/workflows/tests.yml 解析出测试矩阵——"
              "发布门禁必须能点名每一条腿，拒绝继续", file=sys.stderr)
        return 1
    for name in names:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
