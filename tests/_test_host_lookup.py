"""网关宿主模块的查找规则。

脚本入口把网关叫 __main__，库导入叫 wb_proxy；这里固定优先级、标记属性与
回退顺序，别让某个模块悄悄用到第二份副本。
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_host


def fake(**attrs):
    module = types.ModuleType("fake-gateway")
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


class HostLookupTests(unittest.TestCase):
    def setUp(self):
        self.saved = {name: sys.modules.get(name) for name in ("__main__", "wb_proxy")}
        self.addCleanup(self._restore)

    def _restore(self):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_the_running_main_wins(self):
        main = fake(log=lambda *a, **k: None)
        sys.modules["__main__"] = main
        sys.modules["wb_proxy"] = fake(log=lambda *a, **k: None)
        self.assertIs(wb_host.loaded_gateway("log"), main)

    def test_a_main_without_the_marker_loses(self):
        sys.modules["__main__"] = fake()
        proxy = fake(log=lambda *a, **k: None)
        sys.modules["wb_proxy"] = proxy
        self.assertIs(wb_host.loaded_gateway("log"), proxy)

    def test_every_marker_must_be_present(self):
        sys.modules["__main__"] = fake(log=lambda *a, **k: None)
        sys.modules["wb_proxy"] = fake(log=lambda *a, **k: None)
        self.assertIsNone(wb_host.loaded_gateway("log", "build_upstream_body"))

    def test_no_candidate_yields_none(self):
        sys.modules["__main__"] = fake()
        sys.modules.pop("wb_proxy", None)
        self.assertIsNone(wb_host.loaded_gateway("log"))

    def test_gateway_module_falls_back_to_an_imported_copy(self):
        sys.modules["__main__"] = fake()
        proxy = fake()
        sys.modules["wb_proxy"] = proxy
        self.assertIs(wb_host.gateway_module("log"), proxy)

    def test_empty_markers_never_select_a_module(self):
        """没有标记就无法区分网关与无关模块，一律返回 None。"""
        sys.modules["__main__"] = fake(log=lambda *a, **k: None)
        sys.modules["wb_proxy"] = fake(log=lambda *a, **k: None)
        self.assertIsNone(wb_host.loaded_gateway())

    def test_a_foreign_main_is_not_mistaken_for_the_gateway(self):
        """调度器按 stand_down_active 认网关：无关的 __main__ 不能被选中。"""
        sys.modules["__main__"] = fake(log=lambda *a, **k: None)
        sys.modules.pop("wb_proxy", None)
        self.assertIsNone(wb_host.loaded_gateway("stand_down_active"))

    def test_the_marker_selects_a_gateway_that_has_it(self):
        proxy = fake(stand_down_active=lambda: False)
        sys.modules["__main__"] = fake()
        sys.modules["wb_proxy"] = proxy
        self.assertIs(wb_host.loaded_gateway("stand_down_active"), proxy)

    def test_gateway_module_prefers_a_marked_candidate(self):
        main = fake(log=lambda *a, **k: None)
        sys.modules["__main__"] = main
        sys.modules["wb_proxy"] = fake()
        self.assertIs(wb_host.gateway_module("log"), main)


if __name__ == "__main__":
    unittest.main(verbosity=2)
