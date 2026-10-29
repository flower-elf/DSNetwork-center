"""验证会话亲和长度上限、分页轮换与聊天并发上限的自动扩容契约。

会话消息体单调增长，超过几百条后超大请求会让上游断开连接，``WB_AFFINITY_MAX_MSGS`` 到
阈值即释放单账号绑定；``WB_AFFINITY_PAGE_SIZE`` 决定释放进整个池（0）、保持绑定（1）还是
在一个分页内轮换，本文件验证边界取值，池侧行为见 ``_test_affinity_pages.py``。
``WB_MAX_CONCURRENT_CHAT=auto`` 按可用账号数扩容并发上限。

运行方式：python _test_affinity_length_cap.py，不需要上游凭据，也不访问网络。
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="affinity-cap-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


def conversation(length):
    """构造恰好 length 条消息的会话，稳定前缀在最前面。"""
    msgs = [{"role": "system", "content": "you are a helpful assistant"}]
    for i in range(max(0, length - 1)):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant",
                     "content": "turn %d" % i})
    return msgs


class AffinityLengthCapTests(unittest.TestCase):
    """上限只影响超过阈值的会话。"""

    def setUp(self):
        self._saved = wb_proxy.AFFINITY_MAX_MSGS
        self._saved_page = wb_proxy.AFFINITY_PAGE_SIZE
        # 页大小为 0：释放后回到整个池。
        wb_proxy.AFFINITY_PAGE_SIZE = 0

    def tearDown(self):
        wb_proxy.AFFINITY_MAX_MSGS = self._saved
        wb_proxy.AFFINITY_PAGE_SIZE = self._saved_page

    def test_short_conversations_still_bind(self):
        wb_proxy.AFFINITY_MAX_MSGS = 400
        for length in (1, 2, 10, 399, 400):
            key = wb_proxy.derive_affinity_key(conversation(length))
            self.assertTrue(key, "msgs=%d should keep its affinity key" % length)

    def test_long_conversations_are_released_when_the_page_is_off(self):
        wb_proxy.AFFINITY_MAX_MSGS = 400
        for length in (401, 500, 1245):
            self.assertIsNone(
                wb_proxy.derive_affinity_key(conversation(length)),
                "msgs=%d should be released so the pool can rotate" % length)

    def test_threshold_is_inclusive(self):
        """msgs 等于上限时仍绑定，阈值是包含边界。"""
        wb_proxy.AFFINITY_MAX_MSGS = 3
        self.assertIsNotNone(wb_proxy.derive_affinity_key(conversation(3)))
        self.assertIsNone(wb_proxy.derive_affinity_key(conversation(4)))

    def test_zero_disables_the_cap(self):
        """上限为 0 表示始终绑定。"""
        wb_proxy.AFFINITY_MAX_MSGS = 0
        for length in (1, 400, 5000):
            self.assertIsNotNone(wb_proxy.derive_affinity_key(conversation(length)))

    def test_bound_conversations_keep_a_stable_key(self):
        """同一会话的后续轮次共享同一个 key。"""
        wb_proxy.AFFINITY_MAX_MSGS = 400
        first = wb_proxy.derive_affinity_key(conversation(10))
        later = wb_proxy.derive_affinity_key(conversation(300))
        self.assertEqual(first, later)

    def test_distinct_conversations_do_not_collide(self):
        wb_proxy.AFFINITY_MAX_MSGS = 400
        a = conversation(10)
        b = conversation(10)
        b[1] = {"role": "user", "content": "a different opening turn"}
        self.assertNotEqual(wb_proxy.derive_affinity_key(a),
                            wb_proxy.derive_affinity_key(b))

    def test_degenerate_inputs_are_safe(self):
        wb_proxy.AFFINITY_MAX_MSGS = 400
        self.assertIsNone(wb_proxy.derive_affinity_key(None))
        self.assertIsNone(wb_proxy.derive_affinity_key([]))


class AffinityPageTests(unittest.TestCase):
    """超过上限后，会话在池的一个分页内轮换。"""

    def setUp(self):
        self._saved_msgs = wb_proxy.AFFINITY_MAX_MSGS
        self._saved_page = wb_proxy.AFFINITY_PAGE_SIZE
        wb_proxy.AFFINITY_MAX_MSGS = 400

    def tearDown(self):
        wb_proxy.AFFINITY_MAX_MSGS = self._saved_msgs
        wb_proxy.AFFINITY_PAGE_SIZE = self._saved_page

    def test_a_long_conversation_gets_a_key_and_a_page(self):
        wb_proxy.AFFINITY_PAGE_SIZE = 3
        key, page = wb_proxy.affinity_route(conversation(500))
        self.assertTrue(key and key.startswith("pfx-"))
        self.assertIsNotNone(page, "500 msgs must rotate over a page")
        index, size = page
        self.assertEqual(size, 3)
        self.assertTrue(0 <= index < size, "page index out of range: %r" % (page,))

    def test_the_key_survives_crossing_the_cap(self):
        """同一会话跨过上限前后保持同一个 key。"""
        wb_proxy.AFFINITY_PAGE_SIZE = 3
        short = wb_proxy.derive_affinity_key(conversation(400))
        long_key = wb_proxy.derive_affinity_key(conversation(500))
        self.assertEqual(short, long_key)

    def test_the_page_is_stable_across_turns(self):
        wb_proxy.AFFINITY_PAGE_SIZE = 3
        first = wb_proxy.affinity_route(conversation(401))[1]
        later = wb_proxy.affinity_route(conversation(900))[1]
        self.assertEqual(first, later)

    def test_distinct_conversations_spread_over_pages(self):
        wb_proxy.AFFINITY_PAGE_SIZE = 3
        pages = set()
        for index in range(60):
            msgs = conversation(500)
            msgs[1] = {"role": "user", "content": "conversation %d" % index}
            pages.add(wb_proxy.affinity_route(msgs)[1][0])
        self.assertGreater(len(pages), 1,
                           "every conversation landing on one page would be a "
                           "pin with extra steps")

    def test_page_size_one_pins_instead(self):
        wb_proxy.AFFINITY_PAGE_SIZE = 1
        key, page = wb_proxy.affinity_route(conversation(500))
        self.assertTrue(key, "1 means pinned, so the conversation keeps its key")
        self.assertIsNone(page)

    def test_page_size_zero_keeps_the_pool_rotation(self):
        wb_proxy.AFFINITY_PAGE_SIZE = 0
        key, page = wb_proxy.affinity_route(conversation(500))
        self.assertIsNone(key)
        self.assertIsNone(page)

    def test_switching_session_stickiness_off_releases_everything(self):
        wb_proxy.AFFINITY_PAGE_SIZE = 3
        with mock.patch.object(wb_proxy.wb_settings, "session_affinity",
                               return_value=False):
            for length in (10, 500):
                self.assertEqual(wb_proxy.affinity_route(conversation(length)),
                                 (None, None))

    def test_degenerate_inputs_are_safe(self):
        wb_proxy.AFFINITY_PAGE_SIZE = 3
        for msgs in (None, []):
            self.assertEqual(wb_proxy.affinity_route(msgs), (None, None))


class ChatSlotResizeTests(unittest.TestCase):
    """resize_chat_slots 只提高上限，绝不缩小。"""

    def setUp(self):
        self._auto = wb_proxy.CHAT_SLOTS_AUTO
        self._max = wb_proxy.MAX_CONCURRENT_CHAT
        self._slots = wb_proxy._chat_slots

    def tearDown(self):
        wb_proxy.CHAT_SLOTS_AUTO = self._auto
        wb_proxy.MAX_CONCURRENT_CHAT = self._max
        wb_proxy._chat_slots = self._slots

    def test_auto_sizes_from_the_pool(self):
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        self.assertEqual(wb_proxy.resize_chat_slots(110), 110)
        self.assertEqual(wb_proxy.MAX_CONCURRENT_CHAT, 110)
        # 新信号量确实带有这么多许可。
        acquired = 0
        while wb_proxy._chat_slots.acquire(blocking=False):
            acquired += 1
        self.assertEqual(acquired, 110)
        for _ in range(acquired):
            wb_proxy._chat_slots.release()

    def test_small_pool_falls_back_to_the_floor(self):
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        self.assertEqual(wb_proxy.resize_chat_slots(3), wb_proxy.CHAT_SLOTS_FLOOR)

    def test_never_shrinks(self):
        """缩小上限会让 release 次数超过上限，触发 ValueError。"""
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        wb_proxy.resize_chat_slots(200)
        slots = wb_proxy._chat_slots
        self.assertEqual(wb_proxy.resize_chat_slots(5), 200)
        self.assertIs(wb_proxy._chat_slots, slots)

    def test_fixed_setting_is_a_no_op(self):
        """WB_MAX_CONCURRENT_CHAT 设为数字时保持固定上限。"""
        wb_proxy.CHAT_SLOTS_AUTO = False
        wb_proxy.MAX_CONCURRENT_CHAT = 32
        slots = wb_proxy._chat_slots
        self.assertEqual(wb_proxy.resize_chat_slots(500), 32)
        self.assertEqual(wb_proxy.MAX_CONCURRENT_CHAT, 32)
        self.assertIs(wb_proxy._chat_slots, slots)

    def test_garbage_input_is_ignored(self):
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        self.assertEqual(wb_proxy.resize_chat_slots(None), wb_proxy.CHAT_SLOTS_FLOOR)
        self.assertEqual(wb_proxy.resize_chat_slots("abc"), wb_proxy.CHAT_SLOTS_FLOOR)


class SlotAccountingTests(unittest.TestCase):
    """扩容不能破坏在途计数。"""

    def test_resize_preserves_held_slots(self):
        saved_auto = wb_proxy.CHAT_SLOTS_AUTO
        saved_max = wb_proxy.MAX_CONCURRENT_CHAT
        saved_slots = wb_proxy._chat_slots
        try:
            wb_proxy.CHAT_SLOTS_AUTO = True
            wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
            wb_proxy._chat_slots = threading.BoundedSemaphore(wb_proxy.CHAT_SLOTS_FLOOR)
            self.assertTrue(wb_proxy._chat_slots.acquire(blocking=False))
            wb_proxy.resize_chat_slots(64)
            # 扩容前持有的许可属于旧信号量，在新信号量上释放不能触发异常。
            for _ in range(64):
                self.assertTrue(wb_proxy._chat_slots.acquire(blocking=False))
            for _ in range(64):
                wb_proxy._chat_slots.release()
        finally:
            wb_proxy.CHAT_SLOTS_AUTO = saved_auto
            wb_proxy.MAX_CONCURRENT_CHAT = saved_max
            wb_proxy._chat_slots = saved_slots


if __name__ == "__main__":
    unittest.main(verbosity=2)
