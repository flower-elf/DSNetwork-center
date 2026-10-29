"""启动进程的套件共用的生命周期工具：一个空闲端口与一对受管进程接口。

只放多个套件都要用的东西。网关就绪探测与 HTTP 细节留在 _test_connection_reuse.py。

spawn_managed() 与 stop_managed() 是一对：spawn 总把子进程放进独立的进程组或
会话，stop 只接受这样启动的进程。整棵树的清理因此没有歧义：不存在会弄错的
归属标志，给进程组发信号也波及不到调用方自己的进程组。
"""
import os
import signal
import socket
import subprocess

STOP_TIMEOUT = 10.0


def free_port():
    """当前空闲的回环端口。"""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def spawn_managed(*args, **kwargs):
    """在独立进程组或会话里 Popen 子进程，stop_managed() 才能整棵树收掉。

    Popen 的参数原样透传，两个分组标志由这里设置。
    """
    kwargs.update({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                  if os.name == "nt" else {"start_new_session": True})
    return subprocess.Popen(*args, **kwargs)


def stop_managed(proc, timeout=STOP_TIMEOUT):
    """停掉 spawn_managed() 启动的进程，连同它启动的一切。

    清理成功返回 True。False 表示整树那一步没成：受限主机可以拒绝执行
    （taskkill 回答 Access denied），Windows 上已退出的 pid 也无法再遍历其
    子进程。子进程本身两种情况都会被收走。
    """
    if proc is None:
        return True
    if os.name == "nt":
        if proc.poll() is not None:
            return False
        # 先请求、不肯退再强杀；被拒绝的那一轮绝不等待，否则一次被拒的
        # taskkill 会拖成 10 秒停顿。
        walked = _taskkill(proc.pid, force=False)
        if walked:
            _reap(proc, timeout)
        if proc.poll() is None:
            forced = _taskkill(proc.pid, force=True)
            walked = walked or forced
            if forced:
                _reap(proc, timeout)
        if proc.poll() is None:
            _reap(proc, 5, kill=True)
        return bool(walked and proc.poll() is not None)
    # POSIX：子进程自成进程组，组 id 就是它的 pid；组比组长活得久，因此能
    # 波及被留下的孙进程。
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=timeout)
    except Exception:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    _reap(proc, 5)
    return proc.poll() is not None


def _reap(proc, timeout, kill=False):
    try:
        if kill and proc.poll() is None:
            proc.kill()
        proc.wait(timeout=timeout)
    except Exception:
        pass


def _taskkill(pid, force):
    """taskkill 接受请求时为 True。"""
    args = ["taskkill", "/T", "/PID", str(pid)]
    if force:
        args.insert(1, "/F")
    try:
        done = subprocess.run(args, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        return False
    return done.returncode == 0
