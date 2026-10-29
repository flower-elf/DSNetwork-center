"""请求内的标识符生成。

消息、工具调用与事件 id 只需要在进程内唯一，前缀区分用途。放在单独模块里，
净化、协议互转与网关三处共用同一实现，不各自复制一份。
"""

import uuid


def new_id(prefix):
    """返回 prefix 加 32 位十六进制随机串。"""
    return prefix + uuid.uuid4().hex
