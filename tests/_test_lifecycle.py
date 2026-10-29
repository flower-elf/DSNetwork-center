import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import _lifecycle as life  # noqa: E402

PASS = 0
FAIL = 0

#: 父进程脚本，拉起睡眠中的孙进程并打印其 pid。
SPAWN = ("import subprocess, sys, time\n"
         "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
         "print(child.pid, flush=True)\n")


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


def spawn_wrapper(stay_alive):
    """启动一个被管理的父进程：要么持续等待，要么退出并留下它的子进程。"""
    code = SPAWN + ("time.sleep(120)\n" if stay_alive else "sys.exit(0)\n")
    proc = life.spawn_managed([sys.executable, "-c", code], stdout=subprocess.PIPE,
                              text=True)
    return proc, int(proc.stdout.readline().strip())


def alive(pid):
    """判断该 pid 是否仍在运行。不能用 tasklist：受限主机对它返回“Access denied”，会被当成进程不存在，连当前进程都判为已退出，下方的存活检查就形同虚设。
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_orphan(pid):
    """最后手段：清理本套件故意留下的孤儿进程。taskkill 在受限环境会被拒绝，因此直接调用操作系统接口。
    """
    if os.name == "nt":
        import ctypes
        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


port = life.free_port()
try:
    probe = socket.socket()
    probe.bind(("127.0.0.1", port))
    probe.close()
    check("free_port() hands back a bindable port", True)
except OSError as exc:
    check("free_port() hands back a bindable port", False, str(exc))

proc, grandpid = spawn_wrapper(stay_alive=True)
stopped = life.stop_managed(proc)
time.sleep(0.5)
if stopped:
    check("stop_managed() ends a live process and its grandchild",
          not alive(proc.pid) and not alive(grandpid),
          "parent alive=%s grandchild alive=%s" % (alive(proc.pid), alive(grandpid)))
else:
    # 受限主机可能拒绝进程树遍历，taskkill 在沙箱里返回“Access denied”。此时契约要求：
    # 报告树遍历没有发生，仍要回收交出来的进程且不留残余，所以本套件自行清理孙进程并确认它已退出。
    check("a refused tree walk is reported and the process is still reaped",
          not alive(proc.pid), "process alive=%s" % alive(proc.pid))
kill_orphan(grandpid)
time.sleep(0.5)
check("the probe leaves no process behind",
      not alive(proc.pid) and not alive(grandpid),
      "parent alive=%s grandchild alive=%s" % (alive(proc.pid), alive(grandpid)))

proc, grandpid = spawn_wrapper(stay_alive=False)
proc.wait(timeout=30)
stopped = life.stop_managed(proc)
time.sleep(0.5)
left = alive(grandpid)
if os.name == "nt":
    check("stop_managed() reports it cannot walk a dead pid on Windows",
          stopped is False, "returned %s" % stopped)
else:
    check("stop_managed() reaches a grandchild whose parent already exited",
          stopped and not left, "grandchild alive=%s" % left)
kill_orphan(grandpid)
time.sleep(0.5)
check("the probe leaves no process behind here either",
      not alive(grandpid), "grandchild alive=%s" % alive(grandpid))

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
