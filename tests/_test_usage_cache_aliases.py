"""固定缓存 token 别名的契约。各家上游的缓存命中字段不一致：prompt_tokens_details.cached_tokens、prompt_cache_hit_tokens、cache_read_input_tokens、input_tokens_details.cached_tokens 等，有的应答在一个字段里给真值、其他字段里给 0。只认一个别名的客户端会永远报“无缓存命中”。网关取所有别名里最大的正数，并写回全部别名。用 python tests/run_all.py _test_usage_cache_aliases 运行。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy
import wb_usage as U


class BestCachedTokensTests(unittest.TestCase):
    def test_every_alias_is_read(self):
        cases = [
            {"prompt_tokens_details": {"cached_tokens": 12}},
            {"prompt_cache_hit_tokens": 12},
            {"cache_read_input_tokens": 12},
            {"cached_tokens": 12},
            {"input_tokens_details": {"cached_tokens": 12}},
            {"completion_tokens_details": {"cached_tokens": 12}},
        ]
        for usage in cases:
            self.assertEqual(U._best_cached_tokens(usage), 12, usage)

    def test_zero_aliases_do_not_shadow_a_real_hit(self):
        usage = {"prompt_tokens_details": {"cached_tokens": 0},
                 "cache_read_input_tokens": 5}
        self.assertEqual(U._best_cached_tokens(usage), 5)

    def test_missing_or_broken_usage_is_zero(self):
        self.assertEqual(U._best_cached_tokens(None), 0)
        self.assertEqual(U._best_cached_tokens({}), 0)
        self.assertEqual(U._best_cached_tokens({"cached_tokens": "x"}), 0)


class NormalizeAliasesTests(unittest.TestCase):
    def test_best_value_is_written_into_every_alias(self):
        usage = {"prompt_tokens_details": {"cached_tokens": 42},
                 "cache_read_input_tokens": 0, "cached_tokens": 0}
        U.normalize_usage_cache_aliases(usage)
        self.assertEqual(usage["cached_tokens"], 42)
        self.assertEqual(usage["cache_read_input_tokens"], 42)
        self.assertEqual(usage["prompt_cache_hit_tokens"], 42)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 42)

    def test_input_details_are_updated_only_when_present(self):
        usage = {"cached_tokens": 7}
        U.normalize_usage_cache_aliases(usage)
        self.assertNotIn("input_tokens_details", usage)
        usage2 = {"cached_tokens": 7, "input_tokens_details": {"cached_tokens": 0}}
        U.normalize_usage_cache_aliases(usage2)
        self.assertEqual(usage2["input_tokens_details"]["cached_tokens"], 7)

    def test_no_hit_leaves_the_payload_untouched(self):
        usage = {"cached_tokens": 0, "prompt_tokens": 10}
        before = dict(usage)
        U.normalize_usage_cache_aliases(usage)
        self.assertEqual(usage, before)

    def test_extract_usage_uses_the_best_alias(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 5,
                 "total_tokens": 105, "cached_tokens": 0,
                 "prompt_tokens_details": {"cached_tokens": 80}}
        fields = U._extract_usage(usage)
        self.assertEqual(fields["cached_tokens"], 80)
        self.assertEqual(fields["total_tokens"], 105)


if __name__ == "__main__":
    unittest.main(verbosity=2)
