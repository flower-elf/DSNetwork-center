"""请求内标识符生成：前缀区分用途，id 只需进程内唯一。单独成模块，让净化、
协议互转与网关三处共用同一实现。
"""
import uuid


def new_id(prefix):
    return prefix + uuid.uuid4().hex
