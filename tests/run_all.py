import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")      # 网关模块所在目录
JS_DIR = os.path.join(HERE, "javascript")
TAIL_LINES = 25
DEFAULT_TIMEOUT = 300

# 套件中断原因的首选行：traceback 的末行是真正的异常，日志两端都算候选。
ERROR_HINTS = re.compile(
    r"(Traceback \(most recent call last\)"
    r"|^\s*[\w.]*(Error|Exception)\b"
    r"|^\s*(FAILED|AssertionError)"
    r"|is not a function"
    r"|Cannot read|Cannot find|not defined"
    r"|timed out)", re.I)


def _force_utf8_output():
    """把本进程的标准输出与标准错误固定为 UTF-8。

    子套件以 PYTHONIOENCODING=utf-8 运行，日志按 utf-8 读回；本进程再回显同一批行。
    Windows CI 的默认编码是 cp1252，遇到中文摘要会抛 UnicodeEncodeError。
    errors="replace" 让无法编码的字符降级为 '?'，不会中断整轮运行。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_force_utf8_output()


def suites(pattern):
    names = []
    for folder in (HERE, JS_DIR):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.startswith("_test_") and name.endswith((".py", ".js")):
                names.append(name)
    return [name for name in names if pattern in name]


def command(name):
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        path = os.path.join(JS_DIR, name)
    if name.endswith(".js"):
        return ["node", path]
    return [sys.executable, path]


def read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [line.rstrip() for line in fh if line.strip()]
    except OSError:
        return []


def first_error(lines):
    """从失败套件的输出里挑出最有用的那一行。

    Python 套件以异常收尾；加载期就崩溃的 JS 套件末行只是 "Node.js v20.20.2"，
    原因在十几行之前。优先取最后一条像错误的行，否则取末行。
    """
    for line in reversed(lines):
        if ERROR_HINTS.search(line):
            return line.strip()
    return (lines[-1].strip() if lines else "")


def start_kwargs():
    """让套件独占进程组，便于整棵进程树一起结束。"""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(proc):
    """结束套件及其启动的全部进程。

    只结束直接子进程会留下孙进程：启动 wb_proxy.py 的套件会遗留一个占着端口的网关，
    下一个套件看到的环境就与第一个套件不同。
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            done = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=30)
            if done.returncode != 0:
                # taskkill 拒绝执行：主机受限或进程已退出。
                proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        print("        warning: %s survived the kill" % proc.pid)


def run_one(name, env, timeout, logs_dir):
    """运行一个套件，返回调用方汇报所需的全部信息。"""
    started = time.time()
    handle, log_path = tempfile.mkstemp(suffix=".log", prefix=name + ".")
    os.close(handle)
    timed_out = False
    try:
        with open(log_path, "w", encoding="utf-8") as sink:
            proc = subprocess.Popen(command(name), cwd=ROOT, env=env,
                                    stdout=sink, stderr=subprocess.STDOUT,
                                    **start_kwargs())
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_tree(proc)
                code = -1
        lines = read_lines(log_path)
        kept = None
        if logs_dir:
            kept = os.path.join(logs_dir, name + ".log")
            try:
                shutil.copyfile(log_path, kept)
            except OSError:
                kept = None
        return {
            "name": name,
            "ok": code == 0,
            "code": code,
            "seconds": time.time() - started,
            "timed_out": timed_out,
            "last": (lines[-1].strip() if lines else "")[:90],
            "error": first_error(lines),
            "tail": lines[-TAIL_LINES:],
            "log": kept,
        }
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="run_all.py",
        description="Run every bundled suite and print one line per file.")
    parser.add_argument("pattern", nargs="?", default="",
                        help="only run suites whose name contains this")
    parser.add_argument("--jobs", type=int, default=None, metavar="N",
                        help="suites to run at once (default: min(4, cpus))")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        metavar="SEC",
                        help="per-suite wall clock (default: %(default)s)")
    parser.add_argument("--logs", metavar="DIR", default=None,
                        help="keep every suite's full output in DIR")
    args = parser.parse_args(argv[1:])
    if args.timeout <= 0:
        parser.error("--timeout must be a positive number of seconds")
    if args.jobs is not None and args.jobs <= 0:
        parser.error("--jobs must be a positive number of suites")
    if args.jobs is None:
        args.jobs = min(4, os.cpu_count() or 1)
    return args


def main(argv):
    args = parse_args(argv)
    pattern = args.pattern
    env = dict(os.environ)
    # 套件输出含中文标签，Windows CI 默认 cp1252 会让套件抛 UnicodeEncodeError
    # 而中断：套件以 utf-8 运行、日志按 utf-8 读回，本脚本自身的标准输出由
    # _force_utf8_output 在 import 时固定为 UTF-8。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = os.pathsep.join(
        [APP, ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    have_node = shutil.which("node") is not None

    selected = suites(pattern)
    if not selected:
        # 筛选词打错或套件被删光时按错误退出，避免 0 passed, 0 failed 且退出码为 0 被 CI 当成通过。
        print("  no suite matches %r in %s" % (pattern, HERE))
        return 2

    logs_dir = args.logs
    if logs_dir:
        try:
            os.makedirs(logs_dir, exist_ok=True)
        except OSError as exc:
            print("  cannot create %s: %s" % (logs_dir, exc))
            return 2

    passed, failed, skipped, durations = [], [], [], []
    todo = []
    for name in selected:
        if name.endswith(".js") and not have_node:
            skipped.append(name)
            print("  [skip] %-38s node is not on PATH" % name)
        else:
            todo.append(name)

    def report(item):
        """每个套件一行；失败时附上查看更多输出的位置。"""
        name = item["name"]
        durations.append((item["seconds"], name))
        if item["ok"]:
            passed.append(name)
            summary = item["last"]
        else:
            failed.append(name)
            summary = item["error"] or item["last"]
            if item["timed_out"]:
                summary = "timed out after %ds" % args.timeout
        print("  [%s] %-38s %-52s %5.1fs"
              % ("PASS" if item["ok"] else "FAIL", name, summary[:52], item["seconds"]))
        if not item["ok"]:
            if item["log"]:
                print("        full output: %s" % item["log"])
            else:
                print("        --- last %d lines of %s ---" % (TAIL_LINES, name))
                for line in item["tail"]:
                    print("        " + line)

    started = time.time()
    if args.jobs > 1:
        # 各套件是独立进程，且已先做成互不依赖：各自临时目录、备用端口，
        # 因此可以并行。按完成顺序汇报，整轮跑一分钟也不会毫无输出。
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(run_one, name, env, args.timeout, logs_dir)
                       for name in todo]
            for future in as_completed(futures):
                report(future.result())
    else:
        for name in todo:
            report(run_one(name, env, args.timeout, logs_dir))
    total = time.time() - started

    print("")
    if durations:
        slowest = sorted(durations, reverse=True)[:3]
        print("  slowest: %s"
              % ", ".join("%s %.1fs" % (name, secs) for secs, name in slowest))
    print("  %d passed, %d failed, %d skipped  (%s)"
          % (len(passed), len(failed), len(skipped), ROOT))
    print("  total %.1fs with --jobs %d" % (total, args.jobs))
    if failed:
        print("  failed: %s" % ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
