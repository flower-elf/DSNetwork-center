import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_RUNNER = os.path.join(HERE, "run_all.py")

#: 通过的探针写出的行数多于 runner 在尾部保留的行数，--logs 断言因此能区分
#: 完整输出与最后 25 行。
FILLER_LINES = 40

#: 边界刻意放宽：只需区分被 --timeout 截断与等到探针结束，后者要睡十分钟。
HANG_TIMEOUT = 2
HANG_SECONDS = 600
HANG_BUDGET = 60

#: 测试框架自身对沙箱 runner 设的上界，刻意远低于 HANG_SECONDS：runner 一旦
#: 不遵守自己的挂钟，测试框架绝不能继续等下去。
SANDBOX_BUDGET = 6

#: 孙进程活过本轮检查但活不过整个测试会话，宿主拒绝树杀时也不会留下十分钟
#: 的孤儿进程。
ORPHAN_SECONDS = 120

#: run_all.py 中施加单套件挂钟的那一行。负向用例把它从自己的副本里删掉，
#: 被测 runner 于是不遵守 --timeout，只能由测试框架自身的上界结束运行。
TIMEOUT_ANCHOR = "code = proc.wait(timeout=timeout)"

_WINDOWS = os.name == "nt"

if _WINDOWS:
    _KERNEL32 = ctypes.windll.kernel32
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259
    _TH32CS_SNAPPROCESS = 0x2

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    def _process_tree():
        """{pid: parent pid} 直接取自系统，不调用 tasklist 或 taskkill。"""
        snapshot = _KERNEL32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        table = {}
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = _KERNEL32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = _KERNEL32.Process32Next(snapshot, ctypes.byref(entry))
        _KERNEL32.CloseHandle(snapshot)
        return table

    def alive(pid):
        """用系统接口判断存活，不调用 tasklist。

        受限宿主对 tasklist 只回 Access denied 且不打印任何内容，靠解析该
        输出的辅助函数会把所有 pid 都当成已退出；OpenProcess 与
        GetExitCodeProcess 直接询问进程本身。
        """
        handle = _KERNEL32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION,
                                       False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not _KERNEL32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            _KERNEL32.CloseHandle(handle)

    def terminate_pid(pid):
        """通过系统接口杀掉单个 pid，不调用 taskkill。

        runner 用的就是 taskkill，拒绝它的宿主同样拒绝测试框架；最后一道
        防线不能依赖同一个调用。
        """
        handle = _KERNEL32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(_KERNEL32.TerminateProcess(handle, 1))
        finally:
            _KERNEL32.CloseHandle(handle)
else:
    def _process_tree():
        """{pid: parent pid} 取自 /proc，Linux CI 走这条分支。"""
        return _proc_process_tree()

    def alive(pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def terminate_pid(pid):
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False


def _parse_proc_stat(data):
    """从一行 /proc/<pid>/stat 里取出父进程 pid。

    按最后一个 ')' 切分而非按空白切分：命令名位于成对括号中，自身可能含
    空格和括号，其后字段位置固定，先是 state，再是 ppid。
    """
    rest = data.rsplit(b")", 1)[1].split()
    return int(rest[1])


def _proc_process_tree(proc_root="/proc"):
    """从 /proc 形状的树里读出 {pid: parent pid}。

    根目录作为参数，Linux 分支因此能在任意平台上用合成树验证，不必等 CI 才
    暴露。返回形状必须与 Windows 分支一致：_descendants 只读一份映射，
    不应关心它由哪个平台填充。
    """
    try:
        names = [n for n in os.listdir(proc_root) if n.isdigit()]
    except OSError:
        return None
    table = {}
    for name in names:
        try:
            with open(os.path.join(proc_root, name, "stat"), "rb") as fh:
                table[int(name)] = _parse_proc_stat(fh.read())
        except (OSError, IndexError, ValueError):
            continue
    return table


def _descendants(root, table=None):
    """返回 root 之下的全部 pid，先深后浅，root 自身在最后。

    Windows 在父进程退出后仍保留子进程父字段里的创建者 pid，遍历因此仍能
    找到父进程已消失的孤儿。POSIX 上这依赖 /proc；没有 /proc 时只有调用方
    的进程组杀可用，探针因此同时登记自己的 pid。

    `table` 是 {pid: parent pid} 映射，让遍历可以脱离当前平台测试。
    """
    if table is None:
        table = _process_tree()
    if table is None:
        return [root]
    children = {}
    for pid, parent in table.items():
        children.setdefault(parent, []).append(pid)
    levels, frontier, seen = [], [root], {root}
    while frontier:
        levels.append(frontier)
        nxt = []
        for pid in frontier:
            for child in children.get(pid, []):
                if child not in seen:
                    seen.add(child)
                    nxt.append(child)
        frontier = nxt
    order = []
    for level in reversed(levels):
        order.extend(level)
    return order


def new_group_kwargs():
    """让子进程处于自己的进程组或会话。

    两处用法目的一致：测试框架把沙箱 runner 收进自己的组，以便收回整棵
    进程树；run_all.py 给每个套件独立分组也是同一原因。
    """
    if _WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_owned_tree(proc):
    """终止测试框架持有的 runner 及其整棵进程树，然后回收。

    刻意不依赖被测代码和 taskkill：被测 runner 不遵守自身超时、宿主又拒绝其
    树杀时，走的正是这条路径。

    本函数从不抛出异常。它运行在已经失败的路径上，此处抛异常会跳过后续清理，
    恰好留下本函数要消除的孤儿进程。
    """
    if proc.poll() is not None:
        return
    try:
        if not _WINDOWS:
            # runner 自领会话，它的进程组能覆盖未另行分离的进程。
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
        for pid in _descendants(proc.pid):
            terminate_pid(pid)
    except Exception:
        pass
    terminate_pid(proc.pid)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def wait_gone(pid, seconds=10.0):
    """pid 消失后返回 True；进程被杀后不会立刻消失。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.2)
    return not alive(pid)


def read_pids(path, seconds=15.0):
    """读取探针登记的 pid，必要时等待文件出现。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            time.sleep(0.1)
    raise AssertionError("the probe never registered its pids in %s" % path)


#: 一个父进程：启动孙进程、登记两个 pid，然后等待。它既用作挂起探针，也
#: 原样用来询问宿主允许什么。孙进程的工作目录设在场景树之外，宿主杀不掉它
#: 时场景目录仍可删除。
TREE_PROBE = (
    "import json, os, subprocess, sys, tempfile, time\n"
    "child = subprocess.Popen([sys.executable, '-c',\n"
    "    'import time; time.sleep(%d)'], cwd=tempfile.gettempdir())\n"
    "with open(os.environ['PROBE_PID_FILE'], 'w', encoding='utf-8') as fh:\n"
    "    json.dump({'parent': os.getpid(), 'child': child.pid}, fh)\n"
    "print('probe started', flush=True)\n"
    "time.sleep(%d)\n"
)


def host_can_kill_trees():
    """询问当前宿主 runner 的树杀是否真的有效。

    返回 can 与 reason 两个值。探针对一对临时的父进程和孙进程重放
    run_all.kill_tree 的行为：Windows 上用 taskkill /F /T，其余平台用
    killpg。受限宿主会拒绝，taskkill 只回 Access denied，runner 于是退回只
    杀直接子进程；调用方必须知道自己面对的是哪种情况，否则被拒绝的杀进程
    会被当成清理成功。

    探针自身的清理不使用 taskkill：那正是被测量的调用，也可能被拒绝。
    """
    env = dict(os.environ)
    handle, pid_file = tempfile.mkstemp(prefix="runall-tree-", suffix=".json")
    os.close(handle)
    os.unlink(pid_file)
    env["PROBE_PID_FILE"] = pid_file
    proc = subprocess.Popen([sys.executable, "-c", TREE_PROBE % (ORPHAN_SECONDS,
                                                                 ORPHAN_SECONDS)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=tempfile.gettempdir(), env=env,
                            **new_group_kwargs())
    grand = None
    try:
        grand = read_pids(pid_file)["child"]

        permitted = False
        if _WINDOWS:
            done = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=30)
            permitted = done.returncode == 0
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                permitted = True
            except OSError:
                permitted = False

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if not permitted:
            return False, "this host refuses the tree kill"
        if wait_gone(grand, 5):
            return True, "the tree kill removed parent and grandchild"
        return False, "the tree kill was accepted but the grandchild survived"
    finally:
        kill_owned_tree(proc)
        if grand is not None and alive(grand):
            terminate_pid(grand)
        try:
            os.unlink(pid_file)
        except OSError:
            pass


class SandboxTimeout(Exception):
    """沙箱 runner 超过了测试框架自身的上界。"""


class Scenario(object):
    """一次性 tests/ 目录：runner 副本外加若干探针套件。"""

    def __init__(self, label, pids_root):
        self.uid = "%s-%s" % (label, uuid.uuid4().hex[:8])
        self.dir = tempfile.mkdtemp(prefix="runall-contracts-")
        self.tests = os.path.join(self.dir, "tests")
        os.makedirs(self.tests)
        self.runner = os.path.join(self.tests, "run_all.py")
        shutil.copyfile(REAL_RUNNER, self.runner)
        self.logs_dir = os.path.join(self.dir, "kept-logs")
        #: 探针登记的 pid 放在场景目录旁边而非其中：tearDown 要在运行结束后、
        #: 文件删除前读取它们，而探针可能比本次运行活得更久。
        self.pids_dir = os.path.join(pids_root, self.uid)
        os.makedirs(self.pids_dir)
        self.owned = []
        self.names = []
        self._runs = 0

    def add(self, label, body):
        """写入一个探针套件并返回文件名。"""
        name = "_test_%s_%s.py" % (self.uid, label)
        with open(os.path.join(self.tests, name), "w", encoding="utf-8") as fh:
            fh.write(body)
        self.names.append(name)
        return name

    def pid_file(self, label="probe"):
        """探针登记 pid 的位置，测试框架可能要杀掉这些进程。"""
        return os.path.join(self.pids_dir, label + ".json")

    def break_timeout(self):
        """破坏 runner 副本，让它的 --timeout 永不触发。

        锚定精确行，锚点消失时立即报错：负向用例若悄悄失效，比没有该用例
        更糟。
        """
        with open(self.runner, encoding="utf-8") as fh:
            text = fh.read()
        broken = text.replace(TIMEOUT_ANCHOR, "code = proc.wait()", 1)
        if broken == text:
            raise AssertionError(
                "cannot break the copied runner: %r is no longer in run_all.py, "
                "so the negative harness check would prove nothing"
                % TIMEOUT_ANCHOR)
        with open(self.runner, "w", encoding="utf-8") as fh:
            fh.write(broken)

    def run(self, *extra, **kwargs):
        """运行沙箱 runner，返回 returncode、output 与耗时秒数。

        runner 由测试框架在自有进程组中以 Popen 启动，而不用
        subprocess.run 的 timeout 参数：后者只约束直接子进程，runner 一旦
        不遵守 --timeout，探针树就会被留下。这里的外层上界会拿下整棵树并
        在抛出前完成回收。

        输出写入文件而非管道：否则活过本次运行的探针会一直持有管道，让读取
        方挂起。
        """
        timeout = kwargs.pop("timeout", 180)
        env = dict(os.environ)
        env.update(kwargs.pop("env", {}))
        self._runs += 1
        out_path = os.path.join(self.dir, "runner-output-%d.txt" % self._runs)
        started = time.time()
        with open(out_path, "w", encoding="utf-8") as sink:
            proc = subprocess.Popen([sys.executable, self.runner] + list(extra),
                                    cwd=self.dir, env=env, stdout=sink,
                                    stderr=subprocess.STDOUT,
                                    **new_group_kwargs())
            self.owned.append(proc)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_owned_tree(proc)
                # run_all.py 给每个套件独立分组，探针不在 runner 的组里；
                # 只能通过它登记的 pid 找到它。
                self.kill_registered()
                raise SandboxTimeout(
                    "the sandbox runner did not return within %ds: %s"
                    % (timeout, " ".join(extra)))
        seconds = time.time() - started
        with open(out_path, encoding="utf-8", errors="replace") as fh:
            return proc.returncode, fh.read(), seconds

    def kill_registered(self):
        """终止所有登记过 pid 且仍在运行的探针。

        按先深后浅的顺序：父进程消失后，Windows 上的树遍历就找不到孙进程。
        """
        try:
            entries = sorted(os.listdir(self.pids_dir))
        except OSError:
            return
        for entry in entries:
            try:
                with open(os.path.join(self.pids_dir, entry),
                          encoding="utf-8") as fh:
                    pids = json.load(fh)
            except (OSError, ValueError):
                continue
            for key in ("child", "parent"):
                pid = pids.get(key)
                if pid and alive(pid):
                    terminate_pid(pid)

    def stop_owned(self):
        for proc in self.owned:
            kill_owned_tree(proc)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.pids_dir, ignore_errors=True)


class RunnerContractTests(unittest.TestCase):
    def setUp(self):
        self._scenarios = []
        self._pids_root = tempfile.mkdtemp(prefix="runall-contract-pids-")

    def tearDown(self):
        # 顺序不能换：测试框架先收回自己的进程，再删除用来查找进程的文件。
        for scenario in self._scenarios:
            scenario.stop_owned()
            scenario.kill_registered()
            scenario.cleanup()
        shutil.rmtree(self._pids_root, ignore_errors=True)

    def scenario(self, label):
        created = Scenario(label, self._pids_root)
        self._scenarios.append(created)
        return created

    # -- 受测的 runner 副本 ------------------------------------------------

    def test_the_sandbox_runner_is_the_real_one(self):
        """副本与仓库的 run_all.py 逐字节一致，其他检查才有意义。"""
        scenario = self.scenario("identity")
        with open(REAL_RUNNER, "rb") as real, open(scenario.runner, "rb") as copy:
            self.assertEqual(real.read(), copy.read(),
                             "the sandbox runner is not the repository's run_all.py")

    # -- 清理依赖的进程树遍历 ----------------------------------------------

    def test_the_process_table_is_pid_to_parent(self):
        """两个平台分支必须给 _descendants 同一种映射形状。

        Windows 走 Toolhelp32，POSIX 解析 /proc。两者不一致时只在另一个
        平台上暴露，清理路径就是这样悄悄失效的。
        """
        table = _process_tree()
        if table is None:
            self.skipTest("this host exposes no process table")
        self.assertTrue(table, "the process table came back empty")
        for pid, parent in table.items():
            self.assertIsInstance(pid, int, "a process-table key is not an int")
            self.assertIsInstance(parent, int,
                                  "the parent of pid %r is not an int" % (pid,))
        self.assertIn(os.getpid(), table,
                      "the process table does not list this process")
        self.assertIn(os.getpid(), _descendants(os.getpid()),
                      "_descendants dropped the root it was given")

    def test_the_proc_stat_parser_survives_an_awkward_command_name(self):
        """Linux 分支靠这条用例检查，本机 Windows 运行走不到。"""
        line = b"4242 (python (weird) name) S 99 4242 4242 0 -1 4194560 0 0"
        self.assertEqual(_parse_proc_stat(line), 99)
        self.assertEqual(_parse_proc_stat(b"7 (sh) R 1 7 7 0 -1 0 0"), 1)

    def test_the_proc_reader_gives_the_same_shape_as_the_other_platform(self):
        """用合成的 /proc 在所有平台检查 Linux 读取分支。

        两个分支给出的 {pid: parent pid} 映射必须一致，不一致只会在另一个
        平台上暴露。
        """
        root = tempfile.mkdtemp(prefix="fake-proc-")
        try:
            for pid, ppid in ((11, 1), (22, 11), (33, 11)):
                os.makedirs(os.path.join(root, str(pid)))
                with open(os.path.join(root, str(pid), "stat"), "wb") as fh:
                    fh.write(b"%d (python) S %d %d %d 0 -1 0 0"
                             % (pid, ppid, pid, pid))
            # 这两项都不能出现在结果里：非数字目录，以及无法解析的 stat 文件。
            os.makedirs(os.path.join(root, "not-a-pid"))
            os.makedirs(os.path.join(root, "44"))
            with open(os.path.join(root, "44", "stat"), "wb") as fh:
                fh.write(b"garbage")

            table = _proc_process_tree(root)
            self.assertEqual(table, {11: 1, 22: 11, 33: 11})
            self.assertEqual(_descendants(11, table), [22, 33, 11])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_the_proc_reader_reports_no_table_when_proc_is_absent(self):
        """没有 /proc 时返回 None，供调用方退回进程组清理；macOS 走这条分支。"""
        self.assertIsNone(_proc_process_tree(os.path.join(tempfile.gettempdir(),
                                                          "no-such-proc-root")))

    def test_descendants_are_returned_deepest_first(self):
        """顺序决定杀进程后还能找到谁。

        先杀父进程，它的子进程就无人认领，Windows 上的树遍历再也找不到。
        """
        table = {10: 1, 11: 10, 12: 11, 13: 10, 99: 1}
        self.assertEqual(_descendants(10, table), [12, 11, 13, 10])
        self.assertEqual(_descendants(12, table), [12])
        self.assertEqual(_descendants(77, table), [77])

    # -- 参数校验 ----------------------------------------------------------

    def test_a_non_positive_jobs_value_is_a_usage_error(self):
        scenario = self.scenario("jobs")
        for bad in ("0", "-3"):
            with self.subTest(jobs=bad):
                code, output, _ = scenario.run("--jobs", bad, timeout=60)
                self.assertEqual(code, 2, "expected a usage error:\n" + output)
                self.assertIn("--jobs must be a positive number of suites", output)
                self.assertIn("usage:", output)

    def test_a_non_positive_timeout_is_a_usage_error(self):
        scenario = self.scenario("timeout")
        for bad in ("0", "-1"):
            with self.subTest(timeout=bad):
                code, output, _ = scenario.run("--timeout", bad, timeout=60)
                self.assertEqual(code, 2, "expected a usage error:\n" + output)
                self.assertIn("--timeout must be a positive number of seconds",
                              output)

    def test_a_pattern_that_matches_nothing_is_not_a_pass(self):
        """打错筛选词不能报成“0 passed, 0 failed”并以 0 退出。"""
        scenario = self.scenario("nomatch")
        code, output, _ = scenario.run("no-such-suite-" + uuid.uuid4().hex,
                                       timeout=60)
        self.assertEqual(code, 2, output)
        self.assertIn("no suite matches", output)
        self.assertNotIn("0 passed", output)

    # -- 失败汇报 ----------------------------------------------------------

    def test_a_failing_probe_reports_the_real_error(self):
        """探针真实运行并以非零码退出；没有使用任何模拟。"""
        scenario = self.scenario("failing")
        name = scenario.add("fail", (
            "import sys\n"
            "print('probe is running')\n"
            "raise RuntimeError('probe-boom-%s')\n" % scenario.uid))
        code, output, _ = scenario.run(scenario.uid, timeout=120)
        self.assertEqual(code, 1, output)
        self.assertIn("[FAIL] %s" % name, output)
        self.assertIn("RuntimeError: probe-boom-%s" % scenario.uid, output)
        self.assertIn("0 passed, 1 failed", output)
        self.assertIn("failed: %s" % name, output)
        self.assertIn("--- last 25 lines of %s ---" % name, output)

    def test_the_failure_line_survives_a_node_style_tail(self):
        """JS 套件失败时末行只有“Node.js v20.20.2”，报错行必须写出原因。"""
        scenario = self.scenario("nodetail")
        name = scenario.add("nodetail", (
            "import sys\n"
            "print('Traceback (most recent call last):')\n"
            "print('  File \"probe.js\", line 1, in <module>')\n"
            "print('TypeError: probe-not-a-function-%s')\n"
            "print('Node.js v20.20.2')\n"
            "sys.exit(1)\n" % scenario.uid))
        code, output, _ = scenario.run(scenario.uid, timeout=120)
        self.assertEqual(code, 1, output)
        line = [l for l in output.splitlines() if "[FAIL]" in l and name in l]
        self.assertEqual(len(line), 1, output)
        self.assertIn("TypeError: probe-not-a-function-%s" % scenario.uid,
                      line[0])
        self.assertNotIn("Node.js v20.20.2", line[0])

    # -- 日志留存 ----------------------------------------------------------

    def test_logs_dir_keeps_the_full_output_not_just_the_tail(self):
        scenario = self.scenario("logs")
        sentinel = "first-line-%s" % scenario.uid
        name = scenario.add("verbose", (
            "import sys\n"
            "print('%s')\n"
            "for i in range(%d):\n"
            "    print('filler %%d' %% i)\n"
            "print('probe-summary-%s')\n" % (sentinel, FILLER_LINES, scenario.uid)))
        code, output, _ = scenario.run(scenario.uid, "--logs", scenario.logs_dir,
                                       timeout=120)
        self.assertEqual(code, 0, output)
        kept = os.path.join(scenario.logs_dir, name + ".log")
        self.assertTrue(os.path.exists(kept), "no log kept in %s" % scenario.logs_dir)
        with open(kept, encoding="utf-8") as fh:
            kept_text = fh.read()
        # runner 对通过的套件只回显最后一行，因此首行可以证明留存日志是完整输出。
        self.assertNotIn(sentinel, output)
        self.assertIn(sentinel, kept_text)
        self.assertIn("filler %d" % (FILLER_LINES - 1), kept_text)
        self.assertIn("probe-summary-%s" % scenario.uid, kept_text)

    def test_a_failed_probe_points_at_its_kept_log(self):
        scenario = self.scenario("logsfail")
        name = scenario.add("logfail", (
            "import sys\n"
            "print('probe is running')\n"
            "sys.exit(3)\n"))
        code, output, _ = scenario.run(scenario.uid, "--logs", scenario.logs_dir,
                                       timeout=120)
        self.assertEqual(code, 1, output)
        self.assertIn("full output: %s" % os.path.join(scenario.logs_dir,
                                                       name + ".log"), output)

    # -- 挂钟超时 ----------------------------------------------------------

    def test_a_hanging_probe_is_bounded_and_its_tree_is_reaped(self):
        """探针要睡十分钟；必须由 --timeout 结束它。"""
        scenario = self.scenario("hang")
        name = scenario.add("hang", TREE_PROBE % (ORPHAN_SECONDS, HANG_SECONDS))
        pid_file = scenario.pid_file("hang")
        code, output, seconds = scenario.run(scenario.uid, "--timeout",
                                             str(HANG_TIMEOUT), timeout=HANG_BUDGET + 60,
                                             env={"PROBE_PID_FILE": pid_file})
        self.assertEqual(code, 1, output)
        self.assertIn("timed out after %ds" % HANG_TIMEOUT, output)
        self.assertIn("[FAIL] %s" % name, output)
        self.assertLess(seconds, HANG_BUDGET,
                        "run_all.py was not bounded by --timeout: %.1fs" % seconds)

        pids = read_pids(pid_file)
        parent, grand = pids["parent"], pids["child"]

        can, why = host_can_kill_trees()
        if can:
            self.assertTrue(wait_gone(grand, 10),
                            "the timed-out suite's grandchild %d survived; "
                            "tree cleanup did not happen" % grand)
        else:
            # 宿主拒绝树杀时 runner 退回只杀直接子进程，本机无法检查后代进程。
            # 必须打印说明，不能把被拒绝的杀进程当作通过。
            print("  [NOTE] %s; only the direct child is checked here" % why,
                  flush=True)
        try:
            self.assertTrue(wait_gone(parent, 10),
                            "the timed-out suite %d was not reaped" % parent)
        finally:
            if alive(grand):
                terminate_pid(grand)

    def test_a_broken_runner_is_abandoned_boundedly_and_leaves_nothing(self):
        """测试框架必须在 runner 不配合时独立完成清理。

        副本被改成挂钟永不触发，探针睡十分钟，只有测试框架自己的上界能结束
        运行。结束时必须连探针和孙进程一起收掉，否则泄漏进程的正是本套件的
        失败路径。
        """
        scenario = self.scenario("broken")
        scenario.break_timeout()
        scenario.add("hang", TREE_PROBE % (ORPHAN_SECONDS, HANG_SECONDS))
        pid_file = scenario.pid_file("broken")

        started = time.time()
        with self.assertRaises(SandboxTimeout):
            scenario.run(scenario.uid, "--timeout", str(HANG_TIMEOUT),
                         timeout=SANDBOX_BUDGET,
                         env={"PROBE_PID_FILE": pid_file})
        elapsed = time.time() - started
        self.assertLess(elapsed, SANDBOX_BUDGET + 30,
                        "the harness did not give up boundedly: %.1fs" % elapsed)

        pids = read_pids(pid_file)
        self.assertTrue(wait_gone(pids["parent"], 10),
                        "the probe %d outlived the harness timeout"
                        % pids["parent"])
        self.assertTrue(wait_gone(pids["child"], 10),
                        "the grandchild %d outlived the harness timeout"
                        % pids["child"])

    # -- 串行与并行结果一致 ------------------------------------------------

    def test_jobs_1_and_4_agree_on_the_pass_fail_set(self):
        scenario = self.scenario("jobsagree")
        for label in ("alpha", "beta"):
            scenario.add(label, "print('probe-%s-ok')\n" % label)
        for label in ("gamma", "delta"):
            scenario.add(label, (
                "import sys\n"
                "print('probe-%s-running')\n"
                "raise RuntimeError('probe-%s-boom')\n" % (label, label)))

        serial_code, serial, _ = scenario.run(scenario.uid, "--jobs", "1",
                                              timeout=180)
        parallel_code, parallel, _ = scenario.run(scenario.uid, "--jobs", "4",
                                                  timeout=180)

        self.assertEqual(serial_code, 1, serial)
        self.assertEqual(parallel_code, 1, parallel)
        self.assertIn("2 passed, 2 failed", serial)
        self.assertIn("2 passed, 2 failed", parallel)
        # --jobs 大于 1 时完成顺序不固定，因此比较集合。
        self.assertEqual(_failed_set(serial), _failed_set(parallel),
                         "serial and parallel disagreed:\n%s\n%s"
                         % (serial, parallel))
        self.assertEqual(_failed_set(serial), set(scenario.names[2:]))
        self.assertIn("--jobs 1", serial)
        self.assertIn("--jobs 4", parallel)


def _failed_set(output):
    for line in output.splitlines():
        if line.strip().startswith("failed:"):
            return {part.strip() for part in line.split(":", 1)[1].split(",")}
    return set()


if __name__ == "__main__":
    unittest.main(verbosity=2)
