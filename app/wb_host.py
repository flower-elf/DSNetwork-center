"""正在运行的网关模块的查找。直接 import wb_proxy 会载入第二份没有账号池的副本，
日志也进不了面板；所有宿主查找都经过这里。
"""
import sys


def loaded_gateway(*markers):
    """已在内存中的网关模块；没有候选同时具备全部标记属性时返回 None。

    先看 __main__ 再看 wb_proxy。标记属性用来排除同名的无关模块（例如端口被别的
    服务占用时那个服务的 __main__）；空标记一律返回 None。
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
