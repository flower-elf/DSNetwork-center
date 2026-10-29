# 出口级瞬时失败退避：同一出口的账号共享连续计数，成功即清零。
import random
import threading
import time

#: 首次瞬时失败的退避秒数；之后逐次翻倍，封顶 EGRESS_BACKOFF_MAX。
EGRESS_BACKOFF_BASE = 1.0
EGRESS_BACKOFF_MAX = 15.0
#: 距上次瞬时失败超过该时长即开始新一轮计数。
EGRESS_STREAK_RESET = 60.0
#: 抖动比例区间：实际退避为计算值的 0.75 到 1.25 倍，避免请求同步重试。
EGRESS_JITTER_LOW = 0.75
EGRESS_JITTER_HIGH = 1.25

_lock = threading.Lock()
#: egress_key 到 [streak, last_hit_epoch] 的映射。
_state = {}


def transient_backoff(streak):
    """连续 streak 次瞬时失败后的退避秒数，不含随机抖动。"""
    step = max(0, int(streak) - 1)
    return min(EGRESS_BACKOFF_BASE * (2 ** min(step, 20)), EGRESS_BACKOFF_MAX)


def _jitter(wait):
    """给退避加抖动；测试可替换成确定值。"""
    return wait * random.uniform(EGRESS_JITTER_LOW, EGRESS_JITTER_HIGH)


def note_transient(key, now=None):
    """记录一次出口级瞬时失败，返回本次应退避的秒数。

    计数器在出口上：同一出口的多个账号一起抖动只拉长这段退避，不各自熔断。
    """
    now = time.time() if now is None else now
    with _lock:
        state = _state.get(key)
        if state is None or now - state[1] > EGRESS_STREAK_RESET:
            state = [0, now]
        state[0] += 1
        state[1] = now
        _state[key] = state
        return _jitter(transient_backoff(state[0]))


def note_success(key):
    """清零该出口的连续计数；与账号级 note_success 同一规则：成功即清零。"""
    with _lock:
        _state.pop(key, None)


def streak(key, now=None):
    """返回该出口当前的连续瞬时失败次数，供测试与排障使用。"""
    now = time.time() if now is None else now
    with _lock:
        state = _state.get(key)
        if state is None or now - state[1] > EGRESS_STREAK_RESET:
            return 0
        return state[0]


def reset():
    """清空全部状态，供测试使用。"""
    with _lock:
        _state.clear()
