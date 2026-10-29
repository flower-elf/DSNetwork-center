import hashlib
import uuid


def derive_id(uid: str, salt: str) -> str:
    """由 uid 与 salt 稳定派生带连字符的 UUID 形状设备标识；同一账号每次调用同值，
    避免随机机器码触发上游风控。版本位与变体位固定成 UUIDv4 形状，与真实客户端的
    machineId 一致。"""
    seed = f"{salt}:{uid or 'anonymous'}"
    h = hashlib.md5(seed.encode("utf-8")).hexdigest()
    h = h[:12] + "4" + h[13:16] + "a" + h[17:]
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def session_id(uid: str) -> str:
    """这一账号的 sessionId；按账号派生。网关一个进程代表几十台设备，进程级取值
    会让所有账号共用同一个关联键；同一账号恒定、账号之间互不相同，等价于每个
    账号一个客户端实例。"""
    return derive_id(uid, "session")


def generate_request_id(uid: str) -> str:
    """X-Request-ID：32 位无连字符 hex，每条随机，与官方客户端一致。真实客户端的
    请求 id 与账号无关；uid 参数只为兼容既有调用点。"""
    return uuid.uuid4().hex
