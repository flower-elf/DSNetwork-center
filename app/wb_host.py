"""正在运行的网关模块的查找。

网关从命令行启动时模块名是 __main__，作为库导入时是 wb_proxy。其他模块需要
网关的日志、请求准备与状态查询，但直接 import wb_proxy 会在脚本入口下载入第二
份副本，那份副本没有账号池，日志也进不了面板。所有宿主查找都经过这里。
"""

import sys


def loaded_gateway(*markers):
    """已在内存中的网关模块；没有任何候选同时具备全部标记属性时返回 None。

    先看 __main__ 再看 wb_proxy：正常部署里只有一个是网关。标记属性用来排除
    同名的无关模块，例如端口被别的服务占用时那个服务的 __main__。空标记一律
    返回 None：没有标记就没有办法把网关和无关模块分开。
    """
    if not markers:
        return None
    for name in ("__main__", "wb_proxy"):
        module = sys.modules.get(name)
        if module is None:
            continue
        if all(hasattr(module, marker) for marker in markers):
            return module
    return None


def gateway_module(*markers):
    """网关模块；都没加载时按 wb_proxy 导入一份。"""
    module = loaded_gateway(*markers)
    if module is not None:
        return module
    module = sys.modules.get("wb_proxy")
    if module is not None:
        return module
    import wb_proxy as module
    return module
