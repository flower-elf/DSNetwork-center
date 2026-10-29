#!/usr/bin/env python3
"""整轮套件跑完后检出目录变脏就失败。

每个套件的临时状态都该留在临时目录里；写进检出目录的套件不会报错，却把改动
留给下一个套件或下一次发版。本脚本把「跑完必须干净」变成 CI 契约。

脏与否由 Git 判定，忽略规则保持权威。有意持久的输出不算脏：run_all.py --logs
的 suite-logs/、__pycache__/、accounts/*.json 以及 .gitignore 里其余条目。
会挂的是被改动或删除的跟踪文件，以及 .gitignore 未覆盖的新文件或新目录。

    python scripts/check_clean_checkout.py

退出码：0 干净，1 变脏，2 检查无法运行。

前提是套件开跑前检出是干净的，actions/checkout 给 CI 的正是这个前提。对开发者的
工作树运行时，报出的就是该工作树里已有的改动，答案与措辞都相同。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# core.quotePath=false 让非 ASCII 路径按原样打印，不用八进制转义，日志里的
# 路径可以直接贴回 shell。
STATUS = ("git", "-c", "core.quotePath=false", "status", "--porcelain")

# porcelain 的第二列是工作树状态，套件能改动的就是它；第一列是暂存区。
# ?? 表示未跟踪，没有专属字母。
KINDS = {
    "M": "modified",
    "D": "deleted",
    "A": "added",
    "R": "renamed",
    "C": "copied",
    "T": "type changed",
    "U": "unmerged",
}


def porcelain():
    """仓库的 `git status --porcelain` 输出；无法运行时返回 None。"""
    try:
        done = subprocess.run(STATUS, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError as exc:
        print("cannot run git: %s" % exc)
        return None
    if done.returncode != 0:
        print("git status failed with %d: %s"
              % (done.returncode, (done.stderr or "").strip()))
        return None
    return done.stdout


def describe(line):
    """一行 porcelain 输出解析成 (kind, path)。"""
    code, path = line[:2], line[3:]
    if code == "??":
        return "untracked", path
    return KINDS.get(code[1].strip() or code[0], "changed"), path


def main():
    out = porcelain()
    if out is None:
        return 2

    dirty = [line.rstrip() for line in out.splitlines() if line.strip()]
    if not dirty:
        print("checkout is clean: the suites left no repository-local state behind")
        return 0

    print("the suites left the checkout dirty - %d path(s):" % len(dirty))
    for line in dirty:
        kind, path = describe(line)
        print("  %-12s %s" % (kind, path))
    print("")
    print("A suite has to keep its scratch state in a temporary directory.")
    print("If an output is meant to persist, add it to .gitignore with a comment")
    print("saying why; do not delete files here to make this pass.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
