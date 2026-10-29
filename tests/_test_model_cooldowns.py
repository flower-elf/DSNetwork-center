"""模型级上游 429 之后的账号与面板状态。用 python _test_model_cooldowns.py 直接运行，不需要凭证，也不联网。
"""
import atexit
import calendar
import email.utils
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error

_startup_dir = tempfile.TemporaryDirectory(prefix="model-cooldowns-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
# 429 路径会往 usage 目录写限额事件与剩余用量样本，这里也要隔离该目录，
# 否则本套件会把记录写进 checkout。
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts
import wb_proxy as proxy
import wb_settings as settings

# 事故环境里的真实 429 形态，来自 issue #70，已脱敏。S1 占记录的 263 条 429 中的
# 229 条，完全不带恢复信息；S2 是唯一可确认为 intl 的无恢复字段正文，日志只保留
# 前 200 字符，下面的 JSON 是把该前缀补成合法 JSON；S3 作为对照，正文写明了恢复
# 时刻。
S1_BARE = "usage exceeds frequency limit"
S2_INTL_14003 = ('{"code":14003,"msg":"too many requests",'
                 '"retry_policy":{"retry":true},'
                 '"displayMsg":{"en":"Too many requests. Please retry later.",'
                 '"zh":"请求过于频繁，请稍后重试。"}}')
# 同一正文在网关侧的形态：网关最多读 600 字符，日志只留了 200，真实正文因此多一
# 段元数据尾巴。accountId 与 credentialId 只是字段名，不构成作用域依据。
S2_INTL_FULL = ('{"code":14003,"msg":"too many requests",'
                '"accountId":"<redacted>","credentialId":"<redacted>",'
                '"requestId":"<redacted>","retry_policy":{"retry":true},'
                '"displayMsg":{"en":"Too many requests. Please retry later.",'
                '"zh":"请求过于频繁，请稍后重试。"},"ts":1790500000}')
S3_INTL_6004 = ('{"code":6004,"msg":"usage exceeds frequency limit, but don\'t '
                'worry, your usage will reset at 2026-10-06 21:00:09 UTC+8, '
                'alternatively, you can switch to the other models to continue '
                'using it."}')


def stub_pool(account):
    """open_upstream 的最小账号池替身：一个账号、没有绑定。"""
    class Pool(object):
        accounts = [account]
        smart_routing = False
        affinity = types.SimpleNamespace(unbind=lambda _key: None,
                                         demote=lambda _key, _uid=None: None)

        def count_ready(self, realm, model=None):
            return sum(a.ready(model=model) for a in self.accounts)

        def pick_for_session(self, realm, session_key=None, exclude=(), model=None,
                             page=None, claim=None):
            return next((a for a in self.accounts
                         if a.uid not in exclude and a.realm == realm
                         and a.ready(model=model)
                         and (claim is None or claim(a))), None)

        def apply_daily_token_limit(self, value=None, usage=None):
            return value or 0

        def apply_remaining_weights(self, weights=None):
            return weights or {}

    return Pool()


class ModelCooldownTests(unittest.TestCase):
    def account(self):
        return accounts.Account({"uid": "synthetic-cn", "realm": "cn", "accessToken": "token"})

    def test_account_snapshot_and_selection(self):
        account = self.account()
        now = time.time()
        account.note_error("429", model="glm-5.3", until=now + 600)
        account.note_error("429", model="glm-5.2", until=now + 60)
        state = account.public()

        self.assertEqual([item["model"] for item in state["modelCooldowns"]],
                         ["glm-5.2", "glm-5.3"])
        self.assertTrue(all(isinstance(item["expiresAt"], int)
                            for item in state["modelCooldowns"]))
        self.assertFalse(state["inCooldown"])
        self.assertTrue(account.ready(model="another-model"))
        self.assertFalse(account.ready(model="glm-5.3"))

        account.clear_error(model="glm-5.3")
        self.assertEqual([item["model"] for item in account.public()["modelCooldowns"]],
                         ["glm-5.2"])
        with account._throttle_lock:
            account.model_cooldowns["glm-5.2"] = time.time() - 1
        self.assertEqual(account.model_cooldowns_snapshot(), [])

        with account._throttle_lock:
            account.cooldown_until = 1000.0
        with mock.patch.object(accounts.time, "time", return_value=1000.0):
            at_deadline = account.public()
        self.assertFalse(at_deadline["inCooldown"])
        self.assertIsNone(at_deadline["cooldownFor"])

    def test_snapshot_does_not_wait_for_token_refresh(self):
        account = self.account()
        account.note_error("429", model="glm-5.3", until=time.time() + 60)
        with account._refresh_lock:
            start = time.monotonic()
            self.assertEqual(account.public()["modelCooldowns"][0]["model"], "glm-5.3")
            self.assertLess(time.monotonic() - start, 1)

    def test_concurrent_updates_and_panel_reads(self):
        account = self.account()
        stop = threading.Event()
        failures = []

        def writer():
            n = 0
            while not stop.is_set():
                account.note_error("429", model="m%d" % (n % 20), until=time.time() + 5)
                account.clear_error(model="m%d" % ((n + 1) % 20))
                n += 1

        def reader():
            try:
                while not stop.is_set():
                    account.public()
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader)
                                                      for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            time.sleep(0.5)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=2)
        self.assertFalse(any(thread.is_alive() for thread in threads), "worker did not stop")
        self.assertEqual(failures, [])

    def drive_one_429(self, account, detail="usage exceeds frequency limit",
                      stub_parser=True, model="glm-5.3", target_realm="cn",
                      retry_after=None):
        """用桩 urlopen 让 open_upstream 撞上上游 429。

        返回桩解析器给出的恢复时刻，调用方据此断言网关记录的冷却。stub_parser=False
        保留真实解析器，未解析正文的用例需要它。retry_after 以秒为单位，经响应头给出
        恢复时刻，对应正文不带时间戳的 429 形态。
        """
        reset = time.time() + 600
        headers = {} if retry_after is None else {"Retry-After": str(int(retry_after))}
        error = urllib.error.HTTPError("https://upstream.invalid", 429, "rate limit", headers,
                                       io.BytesIO(detail.encode("utf-8")))

        class Pool(object):
            accounts = [account]
            affinity = types.SimpleNamespace(unbind=lambda _key: None,
                                             demote=lambda _key, _uid=None: None)

            def count_ready(self, realm, model=None):
                return sum(a.ready(model=model) for a in self.accounts)

            def pick_for_session(self, realm, session_key=None, exclude=(), model=None,
                                 page=None, claim=None):
                return next((a for a in self.accounts if a.uid not in exclude
                             and a.realm == realm and a.ready(model=model)
                             and (claim is None or claim(a))), None)

            def list_public(self):
                return [a.public() for a in self.accounts]

            def apply_daily_token_limit(self, value=None, usage=None):
                # 生产路径在挑选账号前把日额度守卫推进池；这个桩接住调用即可。
                return value or 0

            def apply_daily_credit_limit(self, value=None, credits=None,
                                         free_models=None):
                return value or 0

            def apply_model_daily_token_limit(self, value=None, per_model=None):
                return value or 0

            def apply_remaining_weights(self, weights=None):
                # 剩余用量权重（并入智能分配的评分）也由请求路径推给池；桩只
                # 负责接住调用，没有加权组合时推来的是 None。
                return weights or {}
        old_pool, old_urlopen = proxy.POOL, accounts.urlopen
        old_parser = proxy.parse_rate_limit_reset
        proxy.POOL = stub_pool(account)
        accounts.urlopen = lambda *args, **kwargs: (_ for _ in ()).throw(error)
        if stub_parser:
            # 固定解析结果，让用例只走模型冷却路径，避开真实解析器的时区处理。
            proxy.parse_rate_limit_reset = lambda _detail: reset
        try:
            with self.assertRaises(proxy.RateLimited) as caught:
                proxy.open_upstream({"model": model, "messages": [
                    {"role": "user", "content": "hello"}]}, target_realm=target_realm)
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen
            proxy.parse_rate_limit_reset = old_parser
            error.close()
        self.last_wait = caught.exception.wait
        return reset

    def cap_body(self, reset_wall, code=6004):
        """构造恢复时刻解析为 reset_wall 的 429 正文。

        时间戳用该时刻 UTC+8 的墙上时钟表示，以 gmtime 生成，随附解析器在任何主机
        时区都解析回同一时刻，配方与剩余用量套件里的 cap_detail 相同。夏令时区里
        解析器按标准偏移换算而 mktime 用夏令时偏移，所以返回前用随附解析器校正一次
        时间戳；映射是分段线性的，一次校正即可命中。
        """
        reset = int(reset_wall)
        offset = reset + 8 * 3600
        detail = ""
        for _ in range(3):
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(offset))
            detail = ('{"code":%d,"msg":"usage exceeds frequency limit, but don\'t '
                      'worry, your usage will reset at %s UTC+8, alternatively, you '
                      'can switch to the other models to continue using it."}'
                      % (code, stamp))
            parsed = proxy.parse_rate_limit_reset(detail)
            if parsed == reset:
                break
            offset += reset - parsed
        return detail

    def test_upstream_429_reaches_accounts_payload(self):
        account = self.account()
        # 429 会把恢复时刻持久化进凭证档（_save_account_window），而这条用例的
        # 合成账号没有自己的文件——不隔离账号目录，它会往 checkout 的 accounts/
        # 落一份 synthetic-cn.json，并污染并行跑的 checkpoint 套件（那些套件的
        # 子进程按硬编码路径读到它，realm 指纹就和父进程对不上了）。
        directory = tempfile.mkdtemp(prefix="cap-save-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            reset = self.drive_one_429(account)
        finally:
            proxy.ACCOUNTS_DIR = old_dir
        row = account.public()
        self.assertFalse(row["inCooldown"])
        self.assertEqual(row["modelCooldowns"][0]["model"], "glm-5.3")
        self.assertLess(abs(row["modelCooldowns"][0]["expiresAt"] - reset), 2)
        self.assertTrue(account.ready(model="another-model"))
        # 客户端拿到的 retry_after 来自上游给的重置时间
        self.assertGreater(self.last_wait, 590)

    def test_the_reset_time_is_read_from_both_message_languages(self):
        cn = json.dumps({"code": 6004, "msg": "您的使用量已超出频率限制，将在 "
                         "2026-09-29 21:54:31 UTC+8 重置，您也可以切换其他模型继续使用。",
                         "requestId": "8b62f376c8884291bd9fd3444c6b7bfa"}, ensure_ascii=False)
        en = json.dumps({"code": 6004, "msg": "usage exceeds frequency limit, your "
                         "usage will reset at 2026-09-19 18:29:03 UTC+8"})
        self.assertEqual(proxy.parse_rate_limit_reset(cn),
                         calendar.timegm((2026, 9, 29, 13, 54, 31, 0, 0, 0)))
        self.assertEqual(proxy.parse_rate_limit_reset(en),
                         calendar.timegm((2026, 9, 19, 10, 29, 3, 0, 0, 0)))
        # 不带时区时按上游所在的 UTC+8 理解
        self.assertEqual(proxy.parse_rate_limit_reset("将在 2026-09-29 21:54:31 重置"),
                         calendar.timegm((2026, 9, 29, 13, 54, 31, 0, 0, 0)))
        # 时间只认 msg，不认 requestId 之类的其它字段
        self.assertIsNone(proxy.parse_rate_limit_reset(
            json.dumps({"msg": "busy", "requestId": "2026-09-29 21:54:31"})))

    def test_retry_after_is_used_when_the_body_names_no_time(self):
        start = time.time()
        self.assertAlmostEqual(proxy.parse_rate_limit_reset("{}", "120"), start + 120, delta=2)
        stamp = email.utils.formatdate(start + 300, usegmt=True)
        self.assertAlmostEqual(proxy.parse_rate_limit_reset("", stamp), start + 300, delta=2)
        self.assertIsNone(proxy.parse_rate_limit_reset('{"code": 6004, "msg": "busy"}'))

    def test_a_model_429_records_the_moment_it_started(self):
        """面板要如实显示「限流时刻 + 恢复时间」。

        60s 的短时限流和几小时的 6004 撞线都要带起始时刻，「开始时间」列才不会用
        「—」占位。这里断言非 6004 的模型级 429 同样记下起始时刻，并且冷却清掉后
        不留残影。
        """
        before = time.time()
        account = self._drive_unparsed(S1_BARE, "started-at")
        entry = account.public()["modelCooldowns"][0]
        self.assertIn("startedAt", entry, "模型级 429 要记下起始时刻")
        self.assertGreaterEqual(entry["startedAt"], before - 1)
        self.assertLessEqual(entry["startedAt"], time.time() + 1)
        self.assertLessEqual(entry["startedAt"], entry["expiresAt"])
        self.assertNotIn("cappedAt", entry, "临时限流没有撞线语义")
        # 清掉冷却后起始时刻不能留在快照里（否则下一行的「开始时间」会串）。
        account.note_success(model="glm-5.3")
        self.assertEqual(account.public()["modelCooldowns"], [])
        self.assertNotIn("glm-5.3", account.model_cooldowns_at)

    def test_a_6004_reports_the_cap_moment_as_the_start(self):
        """6004 的起始时刻就是上游撞线时刻，与 cappedAt 一致（同一瞬间）。"""
        account = accounts.Account({"uid": "synthetic-intl", "realm": "intl",
                                    "accessToken": "token"})
        directory = tempfile.mkdtemp(prefix="cap-start-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            detail = self.cap_body(time.time() + 3600)
            self.drive_one_429(account, detail=detail, stub_parser=False,
                               model="deepseek-v4.1-flash", target_realm="intl")
        finally:
            proxy.ACCOUNTS_DIR = old_dir
        entry = account.public()["modelCooldowns"][0]
        self.assertIn("cappedAt", entry)
        self.assertIn("startedAt", entry)
        self.assertLessEqual(abs(entry["startedAt"] - entry["cappedAt"]), 1)
        # keep_caps 只保留 cap 冷却，起始时刻跟着一起留下。
        account.clear_error(model="deepseek-v4.1-flash", keep_caps=True)
        kept = account.public()["modelCooldowns"][0]
        self.assertEqual(kept["startedAt"], entry["startedAt"])
        # 不带 keep_caps 则两者一起清掉。
        account.clear_error(model="deepseek-v4.1-flash")
        self.assertEqual(account.public()["modelCooldowns"], [])
        self.assertNotIn("deepseek-v4.1-flash", account.model_cooldowns_at)

    def test_the_real_bare_429_parks_only_the_model(self):
        """S1：占已记录 429 的 87%，完全不带恢复信息，只停模型，不动账号级阶梯。"""
        account = self._drive_unparsed(S1_BARE, "bare-shape")
        row = account.public()
        self.assertEqual(account.unscoped_streak, 1,
                         "the repeat is counted on the model ladder")
        self.assertEqual(account.soft_streak, 0,
                         "and never on the credential one")
        self.assertFalse(row["inCooldown"])
        self.assertIsNone(row["cooldownFor"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        self.assertLessEqual(row["modelCooldowns"][0]["expiresAt"] - time.time(), 61,
                             "first tier is the short window, not 600s")
        self.assertTrue(account.ready(model="another-model"))

    def test_the_full_intl_14003_body_with_metadata_parks_only_the_model(self):
        """S2 在网关侧的形态：日志记录的前缀加上被截断的元数据尾巴，即 accountId
        与 credentialId 字段。字段名不构成作用域依据，否则最常见的 intl 形态会走
        600s 到 7200s 的账号级阶梯。"""
        self.assertIn('"accountId"', S2_INTL_FULL)
        self.assertIn('"credentialId"', S2_INTL_FULL)
        account = self._drive_unparsed(S2_INTL_FULL, "intl-14003-full")
        row = account.public()
        self.assertEqual(account.soft_streak, 0, "metadata is not scope evidence")
        self.assertFalse(row["inCooldown"])
        self.assertIsNone(row["cooldownFor"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        self.assertTrue(account.ready(model="another-model"))

    def test_the_real_intl_14003_429_parks_only_the_model(self):
        """S2 的 intl 正文：按 code 14003 识别，也按消息文本识别。"""
        account = self._drive_unparsed(S2_INTL_14003, "intl-14003")
        row = account.public()
        self.assertEqual(account.soft_streak, 0)
        self.assertFalse(row["inCooldown"])
        self.assertIsNone(row["cooldownFor"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        self.assertTrue(account.ready(model="another-model"))

    def test_a_parsed_intl_429_still_parks_until_its_clock(self):
        """S3 对照组：正文写明恢复时刻时，仍按该时刻停模型。"""
        expected = proxy.parse_rate_limit_reset(S3_INTL_6004)
        self.assertEqual(expected, 1791291609)
        account = self._drive_unparsed(S3_INTL_6004, "intl-6004")
        row = account.public()
        self.assertEqual(account.soft_streak, 0, "a parsed clock is not a streak")
        self.assertFalse(row["inCooldown"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        # 正文给的时刻对本次运行已经过去，冷却落到 1s 下限；靠这一点与 60s 的未解析
        # 窗口、账号级阶梯区分开。
        self.assertLessEqual(row["modelCooldowns"][0]["expiresAt"] - time.time(), 2)

    def test_an_account_scope_429_with_a_header_clock_parks_the_model(self):
        """正文像账号级、但响应头给了恢复时刻：按恢复时刻停这个模型，不走阶梯。"""
        directory = tempfile.mkdtemp(prefix="cap-header-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            account = self.account()
            started = time.time()
            self.drive_one_429(account, detail='{"msg": "this account is limited"}',
                               stub_parser=False, retry_after=90)
            self.assertEqual(account.soft_streak, 0, "有恢复时刻就不该走账号级阶梯")
            self.assertIn("glm-5.3", account.model_holds)
            row = account.public()["modelCooldowns"][0]
            self.assertEqual(row["model"], "glm-5.3")
            self.assertLessEqual(abs(row["expiresAt"] - (started + 90)), 3)
            saved = json.load(open(os.path.join(directory, "synthetic-cn.json"),
                                   encoding="utf-8"))
            self.assertIn("glm-5.3", saved.get("modelHolds") or {})
        finally:
            proxy.ACCOUNTS_DIR = old_dir

    def _drive_unparsed(self, detail, tag):
        """把一条真实 429 正文送进 open_upstream，保留真实解析器。"""
        directory = tempfile.mkdtemp(prefix="real-429-%s-" % tag)
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            account = self.account()
            self.drive_one_429(account, detail=detail, stub_parser=False)
            return account
        finally:
            proxy.ACCOUNTS_DIR = old_dir

    def test_repeated_real_429s_do_not_amplify_into_an_account_park(self):
        """连着三次 S1 只能是同一个模型上的三个短窗口，不能放大成整个凭证的
        600s、1200s、2400s 阶梯。"""
        directory = tempfile.mkdtemp(prefix="real-429-repeat-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        windows = []
        try:
            account = self.account()
            for round_number in range(3):
                self.drive_one_429(account, detail=S1_BARE, stub_parser=False)
                self.assertIsNone(account.public()["cooldownFor"],
                                  "round %d parked the credential" % round_number)
                self.assertTrue(account.ready(model="another-model"),
                                "round %d took a sibling model down" % round_number)
                windows.append(account.model_cooldowns["glm-5.3"] - time.time())
                # 让窗口过期，模拟生产里同一形态再次到来；必须留下的是连续计数。
                with account._throttle_lock:
                    account.model_cooldowns["glm-5.3"] = time.time() - 1
        finally:
            proxy.ACCOUNTS_DIR = old_dir
        self.assertEqual(account.unscoped_streak, 3)
        self.assertEqual(account.soft_streak, 0, "the credential counter is untouched")
        self.assertLessEqual(windows[0], 61, "first tier stays short")
        self.assertLess(windows[0], windows[1], "repetition escalates")
        self.assertLess(windows[1], windows[2], "repetition keeps escalating")
        self.assertLessEqual(windows[2], 241, "and stays far below the 2h ceiling")

    def test_unscoped_hits_do_not_lift_the_credential_ladder(self):
        """端到端：先四次无作用域的 S1，再一条账号级正文。两条阶梯不共用计数，
        否则账号级阶梯会从 600 * 2**4 即 7200s 上限起步，正确起点是 600s。"""
        directory = tempfile.mkdtemp(prefix="mixed-seq-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            account = self.account()
            for _ in range(4):
                self.drive_one_429(account, detail=S1_BARE, stub_parser=False)
                with account._throttle_lock:
                    account.model_cooldowns["glm-5.3"] = time.time() - 1
            self.assertEqual(account.unscoped_streak, 4)
            self.assertEqual(account.soft_streak, 0)

            self.drive_one_429(account,
                               detail='{"msg":"too many requests for this account"}',
                               stub_parser=False)
        finally:
            proxy.ACCOUNTS_DIR = old_dir
        self.assertEqual(account.soft_streak, 1)
        self.assertEqual(account.unscoped_streak, 4, "the other ladder is untouched")
        self.assertLessEqual(
            account.cooldown_until - time.time(), accounts.SOFT_RATE_BASE + 1,
            "the credential ladder starts at its own base, not at the ceiling")
        self.assertEqual([item["model"] for item in account.public()["modelCooldowns"]],
                         [], "and adds no model window")

    def test_the_auto_switch_setting_is_opt_in(self):
        """新装默认关闭，只有真正的 JSON 布尔值才会打开。"""
        directory = tempfile.mkdtemp(prefix="auto-switch-setting-")
        self.assertFalse(settings.auto_switch_product(directory))
        self.assertTrue(settings.set_auto_switch_product(directory, True))
        self.assertTrue(settings.auto_switch_product(directory))
        self.assertFalse(settings.set_auto_switch_product(directory, False))
        self.assertFalse(settings.auto_switch_product(directory))
        with open(settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump({"auto_switch_product": "false"}, fh)
        self.assertFalse(settings.auto_switch_product(directory),
                         "a hand-edited string must not read as enabled")

    def test_a_429_keeps_the_identity_while_the_setting_is_off(self):
        directory = tempfile.mkdtemp(prefix="auto-switch-off-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        proxy._SWITCH_LOG.clear()
        try:
            account = self.account()
            self.drive_one_429(account)
            self.assertEqual(account.product, "workbuddy")
            self.assertEqual(proxy._SWITCH_LOG, {})
        finally:
            proxy.ACCOUNTS_DIR = old_dir
            proxy._SWITCH_LOG.clear()

    def test_a_429_rotates_the_identity_once_the_setting_is_on(self):
        directory = tempfile.mkdtemp(prefix="auto-switch-on-")
        settings.set_auto_switch_product(directory, True)
        old_dir = proxy.ACCOUNTS_DIR
        old_budget = proxy.MAX_PRODUCT_SWITCHES
        proxy.ACCOUNTS_DIR = directory
        # 只允许切换一次，断言就不受真实预算影响；切换偶数次会回到 workbuddy。
        proxy.MAX_PRODUCT_SWITCHES = 1
        proxy._SWITCH_LOG.clear()
        try:
            account = self.account()
            self.drive_one_429(account)
            self.assertEqual(account.product, "vscode")
            self.assertTrue(proxy._SWITCH_LOG)
        finally:
            proxy.ACCOUNTS_DIR = old_dir
            proxy.MAX_PRODUCT_SWITCHES = old_budget
            proxy._SWITCH_LOG.clear()

    def test_a_6004_keeps_the_disable_across_an_identity_switch(self):
        """端到端：auto-switch 开着时，撞线后账号仍处于禁用。

        429 分支先记 cap 并写凭证档、挂模型冷却，再换身份，换身份会走
        set_product/clear_error 的清理路径。这里断言整条链结束时 cap 与冷却都还在，
        真机 2026-10-10 的四个撞线账号在切换后都被清成了可用。
        """
        directory = tempfile.mkdtemp(prefix="cap-switch-")
        settings.set_auto_switch_product(directory, True)
        old_dir = proxy.ACCOUNTS_DIR
        old_budget = proxy.MAX_PRODUCT_SWITCHES
        proxy.ACCOUNTS_DIR = directory
        proxy.MAX_PRODUCT_SWITCHES = 1
        proxy._SWITCH_LOG.clear()
        try:
            account = self.account()
            reset = time.time() + 600
            self.drive_one_429(account, detail=self.cap_body(reset),
                               model="hy4-preview-f", target_realm="cn")
            # 身份换了，auto-switch 生效⋯⋯
            self.assertEqual(account.product, "vscode")
            self.assertTrue(proxy._SWITCH_LOG)
            # ⋯⋯但禁用必须留下：cap 记录与模型冷却都还在。
            self.assertIn("hy4-preview-f", account.model_caps)
            self.assertFalse(account.ready(model="hy4-preview-f"))
            self.assertEqual([c["model"] for c in account.public()["modelCooldowns"]],
                             ["hy4-preview-f"])
        finally:
            proxy.ACCOUNTS_DIR = old_dir
            proxy.MAX_PRODUCT_SWITCHES = old_budget
            proxy._SWITCH_LOG.clear()

    def test_switching_the_identity_keeps_a_cap_backed_disable(self):
        """set_product 的清理不能把 6004 的禁用一起清掉。

        这条用例复现真机 bug：无论谁调用 set_product，换身份都只清限流窗口，
        恢复期内的 cap 禁用跨身份成立。
        """
        account = self.account()
        account.note_model_cap("hy4-preview-f", time.time() + 600)
        account.note_error("429", model="glm-5.3", until=time.time() + 60)
        self.assertTrue(account.set_product("vscode"))
        self.assertFalse(account.ready(model="hy4-preview-f"),
                         "换身分不能解除上游额度用满的禁用")
        self.assertTrue(account.ready(model="glm-5.3"),
                        "普通限流窗口照清")

    def test_clear_error_keep_caps_spares_only_cap_backed_cooldowns(self):
        account = self.account()
        now = time.time()
        account.note_model_cap("hy4-preview-f", now + 600)
        account.note_error("429", model="glm-5.3", until=now + 60)
        account.clear_error(keep_caps=True)
        self.assertFalse(account.ready(model="hy4-preview-f"),
                         "上游额度用满的禁用要留下")
        self.assertTrue(account.ready(model="glm-5.3"),
                        "普通限流窗口照清")
        account.clear_error()
        self.assertTrue(account.ready(model="hy4-preview-f"),
                        "不带 keep_caps 时仍然整片清掉")

    def test_a_repeated_6004_rearms_a_cleared_cooldown(self):
        account = self.account()
        reset = time.time() + 600
        self.assertTrue(account.note_model_cap("hy4-preview-f", reset))
        account.clear_error()
        self.assertTrue(account.ready(model="hy4-preview-f"))
        # 同一个窗口的第二次 6004：不用重写凭证档（返回 False），但冷却要挂
        # 回来——否则这个窗口就一直不会被禁用。
        self.assertFalse(account.note_model_cap("hy4-preview-f", reset))
        self.assertFalse(account.ready(model="hy4-preview-f"))

    # ------------------------------------------------------------------
    # 限额联动：6004 → 记录（撞线/恢复时刻）→ 跨重启禁用，覆盖所有模型
    # ------------------------------------------------------------------

    def test_note_model_cap_records_any_model(self):
        cn = self.account()
        intl = accounts.Account({"uid": "synthetic-intl", "realm": "intl",
                                 "accessToken": "token"})
        now = time.time()
        # 付费模型与国内版的 deepseek 同样记 cap：上游给了恢复时刻就成立
        self.assertTrue(cn.note_model_cap("glm-5.3", now + 600))
        self.assertTrue(cn.note_model_cap("deepseek-v4.1-flash", now + 600))
        self.assertEqual(sorted(cn.model_caps), ["deepseek-v4.1-flash", "glm-5.3"])
        self.assertTrue(cn.note_model_cap("hy3", now + 600))
        self.assertTrue(intl.note_model_cap("deepseek-v4.1-flash", now + 600))
        # 同一个窗口重复撞线不算新记录（避免每次都重写凭证档）
        self.assertFalse(cn.note_model_cap("hy3", now + 600))
        # 没有恢复时刻可记的 6004 也不写
        self.assertFalse(cn.note_model_cap("hy4-preview-f", 0))
        self.assertNotIn("hy4-preview-f", cn.model_caps)

    def test_a_stored_cap_for_any_model_is_restored(self):
        """凭证档里的 cap 不分模型范围，加载后全部重新挂上。"""
        now = time.time()
        account = accounts.Account({
            "uid": "synthetic-cn", "realm": "cn", "accessToken": "token",
            "modelCaps": {
                "glm-5.3": {"at": now, "reset": now + 600},
                "hy3": {"at": now, "reset": now + 600},
            },
        })
        self.assertEqual(sorted(account.model_caps), ["glm-5.3", "hy3"])
        self.assertEqual(account.restore_model_caps(), ["glm-5.3", "hy3"])
        self.assertFalse(account.ready(model="glm-5.3"))
        self.assertFalse(account.ready(model="hy3"))
        with mock.patch.object(accounts.time, "time", return_value=now + 601):
            self.assertTrue(account.ready(model="hy3"), "恢复时间过后自动解除")

    def test_a_6004_on_a_free_model_survives_a_restart_and_then_expires(self):
        """整条链：6004 → cap 写盘 → 重启（新建池 + load）仍在禁用 → 到点解除。"""
        directory = tempfile.mkdtemp(prefix="cap-restart-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            detail = self.cap_body(time.time() + 3600)
            reset = proxy.parse_rate_limit_reset(detail)
            self.assertIsNotNone(reset, "夹具的 6004 必须带可解析的重置时刻")
            account = accounts.Account({"uid": "synthetic-intl", "realm": "intl",
                                        "accessToken": "token"})
            started = time.time()
            self.drive_one_429(account, detail=detail, stub_parser=False,
                               model="deepseek-v4.1-flash", target_realm="intl")

            cap = account.model_caps["deepseek-v4.1-flash"]
            self.assertLessEqual(abs(cap["reset"] - reset), 2)
            self.assertGreaterEqual(cap["at"], started - 1)
            self.assertLessEqual(cap["at"], time.time())
            # 面板显示的数据源：冷却条目带 cappedAt（撞线时刻）
            entry = next(item for item in account.public()["modelCooldowns"]
                         if item["model"] == "deepseek-v4.1-flash")
            self.assertLessEqual(abs(entry["cappedAt"] - cap["at"]), 2)

            # 持久化：凭证档里带着 modelCaps（RUNTIME_ONLY_FIELDS 不带它）
            saved_path = os.path.join(directory, "synthetic-intl.json")
            self.assertTrue(os.path.exists(saved_path))
            with open(saved_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertIn("deepseek-v4.1-flash", saved.get("modelCaps") or {})

            # 重启 = 新建池 + load()：恢复时间内仍在禁用，选择逻辑跳过该模型
            pool = accounts.AccountPool(directory)
            loaded = pool.load()
            self.assertEqual(len(loaded), 1)
            revived = loaded[0]
            self.assertGreaterEqual(revived.model_cooldowns.get("deepseek-v4.1-flash", 0.0),
                                    reset - 2)
            self.assertFalse(revived.ready(model="deepseek-v4.1-flash"))
            self.assertTrue(revived.ready(model="glm-5.3"), "同账号其他模型不受影响")
            self.assertIsNone(pool.pick(realm="intl", model="deepseek-v4.1-flash"),
                              "重启后选择逻辑仍跳过被禁用的模型")
            self.assertEqual(pool.pick(realm="intl", model="glm-5.3").uid, "synthetic-intl")
            revived_row = next(item for item in revived.public()["modelCooldowns"]
                               if item["model"] == "deepseek-v4.1-flash")
            self.assertIn("cappedAt", revived_row, "重启加载后面板仍能显示撞线时间")
            # 起始时刻是运行时态，重启即清零；撞线时刻另有持久化的 cap 兜底，
            # 面板的「开始时间」列因此仍有值，这是刻意的分工。
            self.assertNotIn("startedAt", revived_row,
                             "startedAt 是运行时态，不该跨重启")

            # 恢复时间过了以后自动解除
            with mock.patch.object(accounts.time, "time", return_value=reset + 1):
                self.assertTrue(revived.ready(model="deepseek-v4.1-flash"))
                self.assertEqual(pool.pick(realm="intl", model="deepseek-v4.1-flash").uid,
                                 "synthetic-intl")
        finally:
            proxy.ACCOUNTS_DIR = old_dir

    def test_a_6004_on_a_paid_model_is_recorded_and_survives_a_restart(self):
        """付费模型（以及另一区域不算免费的模型）收到 6004 同样记 cap 并写盘。"""
        for realm, model in (("cn", "glm-5.3"), ("cn", "deepseek-v4.1-flash"),
                             ("intl", "glm-5.3")):
            directory = tempfile.mkdtemp(prefix="cap-scope-%s-" % realm)
            old_dir = proxy.ACCOUNTS_DIR
            proxy.ACCOUNTS_DIR = directory
            try:
                detail = self.cap_body(time.time() + 3600)
                reset = proxy.parse_rate_limit_reset(detail)
                account = accounts.Account({"uid": "synthetic-%s" % realm,
                                            "realm": realm, "accessToken": "token"})
                self.drive_one_429(account, detail=detail, stub_parser=False,
                                   model=model, target_realm=realm)
                self.assertIn(model, account.model_caps,
                              "%s/%s 的 6004 必须记 cap" % (realm, model))
                row = account.public()["modelCooldowns"][0]
                self.assertEqual(row["model"], model)
                self.assertIn("cappedAt", row)
                self.assertLessEqual(abs(row["expiresAt"] - reset), 2,
                                     "按上游时钟停到点")
                saved_path = os.path.join(directory, "synthetic-%s.json" % realm)
                self.assertTrue(os.path.exists(saved_path), "cap 必须写盘")
                with open(saved_path, encoding="utf-8") as fh:
                    self.assertIn(model, json.load(fh).get("modelCaps") or {})
                # 重启：新建池 + load，仍在恢复期内就不接单
                pool = accounts.AccountPool(directory)
                revived = pool.load()[0]
                self.assertFalse(revived.ready(model=model))
                with mock.patch.object(accounts.time, "time", return_value=reset + 1):
                    self.assertTrue(revived.ready(model=model), "恢复时间过后自动解除")
            finally:
                proxy.ACCOUNTS_DIR = old_dir

    def test_only_code_6004_counts_as_a_cap(self):
        """「达到限额」的唯一标志是 6004：带时钟的 14003 也不是额度判定。"""
        self.assertTrue(proxy.rate_limit_is_cap(S3_INTL_6004))
        self.assertTrue(proxy.rate_limit_is_cap('{"code":"6004","msg":"x"}'),
                        "字符串形式的 code 不能把真 cap 漏掉")
        self.assertFalse(proxy.rate_limit_is_cap(S1_BARE))
        self.assertFalse(proxy.rate_limit_is_cap(S2_INTL_14003))
        self.assertFalse(proxy.rate_limit_is_cap(""))
        self.assertFalse(proxy.rate_limit_is_cap('{"code":null}'))

        directory = tempfile.mkdtemp(prefix="cap-code-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            detail = self.cap_body(time.time() + 3600, code=14003)
            reset = proxy.parse_rate_limit_reset(detail)
            self.assertIsNotNone(reset, "夹具必须带可解析的重置时刻")
            account = accounts.Account({"uid": "synthetic-cn", "realm": "cn",
                                        "accessToken": "token"})
            self.drive_one_429(account, detail=detail, stub_parser=False, model="hy3")
            self.assertEqual(account.model_caps, {}, "非 6004 不记 cap")
            self.assertIn("hy3", account.model_holds, "带恢复时刻的 429 记窗口")
            row = account.public()["modelCooldowns"][0]
            self.assertEqual(row["model"], "hy3")
            self.assertNotIn("cappedAt", row, "普通 429 不是「额度用满」")
            self.assertLessEqual(abs(row["expiresAt"] - reset), 2)
            saved_path = os.path.join(directory, "synthetic-cn.json")
            self.assertTrue(os.path.exists(saved_path), "窗口必须写盘")
            with open(saved_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertEqual(saved.get("modelCaps"), {})
            self.assertIn("hy3", saved.get("modelHolds") or {})
            # 重启后仍然停在这个窗口上
            pool = accounts.AccountPool(directory)
            revived = pool.load()[0]
            self.assertFalse(revived.ready(model="hy3"))
            with mock.patch.object(accounts.time, "time", return_value=reset + 1):
                self.assertTrue(revived.ready(model="hy3"), "恢复时间过后自动解除")
        finally:
            proxy.ACCOUNTS_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
