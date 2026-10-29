"""更新发现：网关可以问 GitHub 什么、可以留什么。

更新功能的第一阶段只做发现：报告存在更新的稳定版就停住。本套件钉住的是
它周围的契约：

  * 每日检查显式开启，从没设置过该键的安装默认关闭；
  * 关闭时一个请求都不发，开启时约 24 小时一次；手动检查不受开关和窗口
    限制；
  * 草稿、预发布、畸形 tag 与意料之外的文档一律跳过，不能让网关崩溃；
  * 检查失败不致命：保留上一次已知答案，报一行短消息，不带 URL 与响应
    正文；
  * 后台线程不能拖住进程退出。

不访问网络：每次检查都跑在注入的 fetcher 上。

    python tests/_test_update_check.py
"""
import email
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)

_TMP = isolated_data_dirs("wb-update-check-")

import wb_proxy as proxy  # noqa: E402
import wb_settings  # noqa: E402
import wb_updates  # noqa: E402

CURRENT = "1.6.17"


def release(tag, **overrides):
    entry = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "html_url": "https://github.com/flower-elf/DSNetwork-center/releases/tag/" + tag,
        "published_at": "2026-10-08T00:00:00Z",
    }
    entry.update(overrides)
    return entry


class FakeFetcher(object):
    """顶替 GitHub 请求，并统计调用次数。"""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload)


def tag_url(tag):
    return ("https://github.com/flower-elf/DSNetwork-center"
            "/releases/tag/" + tag)


class Redirected(object):
    """跟随过重定向的应答：urlopen 只报最终 URL，别的什么都没有。"""

    def __init__(self, url):
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def checker(directory, fetcher, **kwargs):
    kwargs.setdefault("first_delay", 0)
    kwargs.setdefault("poll_seconds", 0.01)
    return wb_updates.UpdateChecker(current_version=CURRENT,
                                    settings_dir=directory,
                                    fetcher=fetcher, **kwargs)


class SettingsContractTests(unittest.TestCase):
    def test_missing_key_reads_as_off(self):
        with tempfile.TemporaryDirectory(prefix="upd-set-") as d:
            self.assertFalse(wb_settings.update_check_enabled(d))
            with mock.patch.object(proxy, "ACCOUNTS_DIR", d):
                self.assertFalse(proxy.runtime_settings_view()["update_check_enabled"])

    def test_enable_and_disable_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="upd-set-") as d:
            self.assertTrue(wb_settings.set_update_check_enabled(d, True))
            self.assertTrue(wb_settings.update_check_enabled(d))
            with open(wb_settings.settings_path(d), encoding="utf-8") as fh:
                self.assertIs(json.load(fh)["update_check_enabled"], True)
            self.assertFalse(wb_settings.set_update_check_enabled(d, False))
            self.assertFalse(wb_settings.update_check_enabled(d))
            with mock.patch.object(proxy, "ACCOUNTS_DIR", d):
                self.assertFalse(proxy.runtime_settings_view()["update_check_enabled"])

    def test_only_a_real_true_enables_it(self):
        # 手改的 "yes" 或 1 不能开启每日外发请求。
        for value in ("yes", 1, "true", [1], {"on": True}):
            with tempfile.TemporaryDirectory(prefix="upd-set-") as d:
                wb_settings.save(d, {"update_check_enabled": value})
                self.assertFalse(wb_settings.update_check_enabled(d), value)

    def test_interval_defaults_to_a_day(self):
        with tempfile.TemporaryDirectory(prefix="upd-set-") as d:
            self.assertEqual(wb_settings.update_check_interval_hours(d), 24)

    def test_interval_round_trip_and_bounds(self):
        with tempfile.TemporaryDirectory(prefix="upd-set-") as d:
            self.assertEqual(wb_settings.set_update_check_interval_hours(d, 6), 6)
            self.assertEqual(wb_settings.update_check_interval_hours(d), 6)
            with self.assertRaises(ValueError):
                wb_settings.set_update_check_interval_hours(d, 0)
            with self.assertRaises(ValueError):
                wb_settings.set_update_check_interval_hours(d, 721)
            wb_settings.save(d, {"update_check_interval_hours": "junk"})
            self.assertEqual(wb_settings.update_check_interval_hours(d), 24)

    def test_env_overrides_enabled_and_interval(self):
        with tempfile.TemporaryDirectory(prefix="upd-set-") as d:
            wb_settings.set_update_check_enabled(d, True)
            wb_settings.set_update_check_interval_hours(d, 48)
            with mock.patch.dict(os.environ, {"WB_UPDATE_CHECK_ENABLED": "0"}):
                self.assertFalse(wb_settings.update_check_enabled(d))
            with mock.patch.dict(os.environ, {"WB_UPDATE_CHECK_INTERVAL_HOURS": "6"}):
                self.assertEqual(wb_settings.update_check_interval_hours(d), 6)
            self.assertEqual(wb_settings.panel_update_check_interval_hours(d), 48)
            with mock.patch.dict(os.environ, {"WB_UPDATE_CHECK_ENABLED": "0",
                                              "WB_UPDATE_CHECK_INTERVAL_HOURS": "6"}):
                overrides = wb_settings.env_overrides()
            self.assertEqual(overrides["update_check_enabled"]["value"], "关闭")
            self.assertEqual(overrides["update_check_interval_hours"]["value"], "6")


class VersionComparisonTests(unittest.TestCase):
    def test_plain_semver_parses_with_or_without_the_v(self):
        self.assertEqual(wb_updates.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(wb_updates.parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(wb_updates.parse_version(" v10.0.4 "), (10, 0, 4))

    def test_unorderable_tags_are_rejected(self):
        for tag in ("v1.2", "v1.2.3.4", "v1.2.3-rc1", "nightly", "", None, "v1.x.3"):
            self.assertIsNone(wb_updates.parse_version(tag), tag)

    def test_is_newer_orders_three_parts(self):
        self.assertTrue(wb_updates.is_newer("1.6.18", "1.6.17"))
        self.assertTrue(wb_updates.is_newer("v1.7.0", "1.6.99"))
        self.assertTrue(wb_updates.is_newer("2.0.0", "1.99.99"))
        self.assertFalse(wb_updates.is_newer("1.6.17", "1.6.17"))
        self.assertFalse(wb_updates.is_newer("1.6.16", "1.6.17"))
        self.assertFalse(wb_updates.is_newer("garbage", "1.6.17"))


class ReleasePickingTests(unittest.TestCase):
    def test_newer_stable_release_wins(self):
        picked = wb_updates.pick_latest([release("v1.6.18")], CURRENT)
        self.assertEqual(picked["version"], "1.6.18")
        self.assertTrue(picked["newer"])
        self.assertTrue(picked["url"].startswith("https://github.com/"))

    def test_same_and_older_releases_are_not_an_update(self):
        for tag in ("v1.6.17", "v1.6.16", "v1.0.0"):
            self.assertFalse(wb_updates.pick_latest([release(tag)], CURRENT)["newer"], tag)

    def test_drafts_and_prereleases_are_ignored(self):
        document = [
            release("v9.9.9", draft=True),
            release("v8.8.8", prerelease=True),
            release("v1.6.18"),
        ]
        self.assertEqual(wb_updates.pick_latest(document, CURRENT)["version"], "1.6.18")

    def test_only_drafts_and_prereleases_means_no_known_latest(self):
        document = [release("v9.9.9", draft=True), release("v8.8.8", prerelease=True)]
        self.assertIsNone(wb_updates.pick_latest(document, CURRENT))

    def test_malformed_entries_are_skipped_not_fatal(self):
        document = [
            "not a release",
            None,
            17,
            {},
            {"tag_name": None},
            {"tag_name": "nightly"},
            {"tag_name": "v1.6.18", "draft": "yes-please"},
            release("v1.6.19"),
        ]
        self.assertEqual(wb_updates.pick_latest(document, CURRENT)["version"], "1.6.19")

    def test_the_highest_version_wins_not_the_first(self):
        document = [release("v1.6.18"), release("v1.9.0"), release("v1.7.5")]
        self.assertEqual(wb_updates.pick_latest(document, CURRENT)["version"], "1.9.0")

    def test_a_non_list_document_picks_nothing(self):
        for document in ({}, {"releases": []}, "text", None):
            self.assertIsNone(wb_updates.pick_latest(document, CURRENT), document)

    def test_release_url_is_restricted_to_github_https(self):
        for bad in ("javascript:alert(1)", "http://github.com/x", "data:text/html,x", ""):
            self.assertEqual(wb_updates.safe_url(bad), "", bad)
        self.assertEqual(wb_updates.safe_url("https://github.com/a/b"),
                         "https://github.com/a/b")


class CheckBehaviourTests(unittest.TestCase):
    def test_disabled_mode_sends_no_request(self):
        with tempfile.TemporaryDirectory(prefix="upd-run-") as d:
            fetcher = FakeFetcher([release("v1.6.18")])
            checker_ = checker(d, fetcher)
            status = checker_.check()
            self.assertEqual(fetcher.calls, [])
            self.assertEqual(status["skipped"], "disabled")
            self.assertFalse(status["enabled"])
            self.assertIsNone(status["latest_version"])

    def test_enabled_mode_respects_the_24h_gate(self):
        with tempfile.TemporaryDirectory(prefix="upd-run-") as d:
            wb_settings.set_update_check_enabled(d, True)
            fetcher = FakeFetcher([release("v1.6.18")])
            checker_ = checker(d, fetcher)
            first = checker_.check()
            self.assertEqual(len(fetcher.calls), 1)
            self.assertTrue(first["update_available"])
            self.assertEqual(first["latest_version"], "1.6.18")
            second = checker_.check()
            self.assertEqual(len(fetcher.calls), 1, "a second pass must wait out the window")
            self.assertEqual(second["skipped"], "not due yet")

    def test_the_window_opens_after_a_day(self):
        with tempfile.TemporaryDirectory(prefix="upd-run-") as d:
            wb_settings.set_update_check_enabled(d, True)
            fetcher = FakeFetcher([release("v1.6.18")])
            checker_ = checker(d, fetcher)
            checker_.check()
            checker_._last_attempt = time.time() - wb_updates.SCHEDULED_INTERVAL_SECONDS - 1
            self.assertTrue(checker_.due())
            checker_.check()
            self.assertEqual(len(fetcher.calls), 2)

    def test_a_custom_interval_moves_the_window(self):
        with tempfile.TemporaryDirectory(prefix="upd-run-") as d:
            wb_settings.set_update_check_enabled(d, True)
            wb_settings.set_update_check_interval_hours(d, 6)
            fetcher = FakeFetcher([release("v1.6.18")])
            checker_ = checker(d, fetcher)
            checker_.check()
            checker_._last_attempt = time.time() - 5 * 3600
            self.assertFalse(checker_.due())
            checker_._last_attempt = time.time() - 7 * 3600
            self.assertTrue(checker_.due())
            checker_.check()
            self.assertEqual(len(fetcher.calls), 2)

    def test_manual_check_ignores_the_switch_and_the_window(self):
        with tempfile.TemporaryDirectory(prefix="upd-run-") as d:
            fetcher = FakeFetcher([release("v1.6.18")])
            checker_ = checker(d, fetcher)
            checker_.check()                      # 关闭：不发请求
            self.assertEqual(fetcher.calls, [])
            status = checker_.check(manual=True)  # 操作者明确要求
            self.assertEqual(len(fetcher.calls), 1)
            self.assertTrue(status["update_available"])
            checker_.check(manual=True)
            self.assertEqual(len(fetcher.calls), 2, "manual bypasses the 24h gate too")

    def test_scheduled_pass_with_nothing_newer_reports_false(self):
        with tempfile.TemporaryDirectory(prefix="upd-run-") as d:
            wb_settings.set_update_check_enabled(d, True)
            fetcher = FakeFetcher([release("v1.6.17"), release("v1.6.10")])
            status = checker(d, fetcher).check()
            self.assertEqual(len(fetcher.calls), 1)
            self.assertFalse(status["update_available"])
            self.assertEqual(status["latest_version"], "1.6.17")
            self.assertEqual(status["last_error"], "")

    def test_cadence_survives_a_restart(self):
        with tempfile.TemporaryDirectory(prefix="upd-run-") as d:
            wb_settings.set_update_check_enabled(d, True)
            fetcher = FakeFetcher([release("v1.6.18")])
            checker(d, fetcher).check()
            # 新进程会读回已存的尝试时刻，重启不能当成立刻再查的理由。
            revived = checker(d, FakeFetcher([release("v1.6.18")]))
            self.assertFalse(revived.due())
            status = revived.status()
            self.assertEqual(status["latest_version"], "1.6.18")
            self.assertTrue(status["update_available"])
            # 重启后还没重新检查时，弹窗照样要有可点的发布页。
            self.assertEqual(status["release_url"], tag_url("v1.6.18"))
            self.assertEqual(status["published_at"], "2026-10-08T00:00:00Z")

    def test_a_result_from_another_repo_is_dropped_on_load(self):
        """换了检查目标后，旧仓库的版本号不能继续当结果，也不能占着节流窗口。"""
        for stored_repo in ("ardeyouxipianyi/workbuddy2api-hub", None):
            with tempfile.TemporaryDirectory(prefix="upd-migrate-") as d:
                wb_settings.set_update_check_enabled(d, True)
                now = time.time()
                entry = {
                    "last_attempt": now,
                    "last_success": now,
                    "latest_version": "1.9.0",
                    "latest_url": "https://github.com/ardeyouxipianyi/workbuddy2api-hub"
                                  "/releases/tag/v1.9.0",
                }
                if stored_repo:
                    entry["repo"] = stored_repo
                with open(wb_settings.settings_path(d), "w", encoding="utf-8") as fh:
                    json.dump({"update_check_enabled": True,
                               wb_settings.UPDATE_CHECK_STATE_KEY: entry}, fh)
                revived = checker(d, FakeFetcher([release("v1.6.18")]))
                status = revived.status()
                self.assertIsNone(status["latest_version"], stored_repo)
                self.assertFalse(status["update_available"], stored_repo)
                self.assertTrue(revived.due(), "旧记录不该占着 24 小时窗口")
                # 新目标的一次检查会写回自己的来源标记。
                revived.check(manual=True)
                self.assertEqual(wb_settings.update_check_state(d)["repo"],
                                 wb_updates.REPO)

    def test_a_failed_check_after_the_repo_change_also_settles(self):
        """迁移后首检失败也要写新来源并作废旧结果，不能每次重启都再查一次。"""
        with tempfile.TemporaryDirectory(prefix="upd-migrate-fail-") as d:
            wb_settings.set_update_check_enabled(d, True)
            now = time.time()
            with open(wb_settings.settings_path(d), "w", encoding="utf-8") as fh:
                json.dump({"update_check_enabled": True,
                           wb_settings.UPDATE_CHECK_STATE_KEY: {
                               "repo": "ardeyouxipianyi/workbuddy2api-hub",
                               "last_attempt": now,
                               "last_success": now,
                               "latest_version": "1.9.0",
                           }}, fh)
            failed = checker(d, FakeFetcher(error=urllib.error.URLError("down")))
            failed.check(manual=True)
            state = wb_settings.update_check_state(d)
            self.assertEqual(state["repo"], wb_updates.REPO)
            self.assertEqual(state["latest_version"], "")
            self.assertEqual(state["latest_url"], "")
            self.assertEqual(state["last_success"], 0.0,
                             "旧仓库的成功时间不能算到新目标头上")
            # 之后的进程读到的是同一个来源：节流窗口恢复了。
            revived = checker(d, FakeFetcher([release("v1.6.18")]))
            self.assertFalse(revived.due())
            self.assertIsNone(revived.status()["latest_version"])


class FailureHandlingTests(unittest.TestCase):
    def _failing(self, directory, error):
        fetcher = FakeFetcher(error=error)
        status = checker(directory, fetcher).check(manual=True)
        return fetcher, status

    def test_http_error_is_reported_without_the_body(self):
        with tempfile.TemporaryDirectory(prefix="upd-fail-") as d:
            error = urllib.error.HTTPError(
                wb_updates.RELEASES_URL, 403, "rate limited",
                {"X-Secret-Header": "leak-me"}, None)
            _fetcher, status = self._failing(d, error)
            self.assertEqual(status["last_error"], "HTTP 403")
            self.assertFalse(status["update_available"])
            self.assertIsNone(status["latest_version"])

    def test_network_error_is_non_fatal(self):
        with tempfile.TemporaryDirectory(prefix="upd-fail-") as d:
            error = urllib.error.URLError("temporary failure in name resolution")
            _fetcher, status = self._failing(d, error)
            self.assertEqual(status["last_error"], "network error: URLError")
            self.assertFalse(status["checking"])

    def test_invalid_json_is_non_fatal(self):
        with tempfile.TemporaryDirectory(prefix="upd-fail-") as d:
            fetcher = FakeFetcher("{ this is not json")
            status = checker(d, fetcher).check(manual=True)
            self.assertEqual(len(fetcher.calls), 1)
            self.assertEqual(status["last_error"], "invalid JSON")

    def test_an_unexpected_document_is_non_fatal(self):
        with tempfile.TemporaryDirectory(prefix="upd-fail-") as d:
            status = checker(d, FakeFetcher({"message": "Not Found"})).check(manual=True)
            self.assertEqual(status["last_error"], "unexpected release document")

    def test_a_failed_check_keeps_the_last_known_answer(self):
        with tempfile.TemporaryDirectory(prefix="upd-fail-") as d:
            wb_settings.set_update_check_enabled(d, True)
            checker_ = checker(d, FakeFetcher([release("v1.6.18")]))
            checker_.check()
            checker_._fetch = FakeFetcher(error=urllib.error.URLError("down"))
            checker_._last_attempt = 0
            status = checker_.check()
            self.assertEqual(status["last_error"], "network error: URLError")
            self.assertEqual(status["latest_version"], "1.6.18")
            self.assertTrue(status["update_available"])

    def test_no_url_or_body_reaches_the_settings_file(self):
        marker = "SECRET-BODY-MARKER"
        with tempfile.TemporaryDirectory(prefix="upd-fail-") as d:
            error = urllib.error.URLError("failed for https://api.github.com/x?token=" + marker)
            checker(d, FakeFetcher(error=error)).check(manual=True)
            with open(wb_settings.settings_path(d), encoding="utf-8") as fh:
                raw = fh.read()
        self.assertNotIn(marker, raw)
        self.assertNotIn("api.github.com", raw)
        self.assertNotIn("https://", raw)

    def test_only_operational_fields_are_persisted(self):
        with tempfile.TemporaryDirectory(prefix="upd-fail-") as d:
            wb_settings.set_update_check_enabled(d, True)
            checker(d, FakeFetcher([release("v1.6.18")])).check()
            with open(wb_settings.settings_path(d), encoding="utf-8") as fh:
                stored = json.load(fh)
        self.assertEqual(sorted(stored["update_check"]),
                         ["last_attempt", "last_success", "latest_url",
                          "latest_version", "published_at", "repo"])
        self.assertEqual(stored["update_check"]["latest_version"], "1.6.18")
        self.assertEqual(stored["update_check"]["latest_url"], tag_url("v1.6.18"))
        self.assertEqual(stored["update_check"]["repo"], wb_updates.REPO)

    def test_a_failed_attempt_is_not_recorded_as_a_success(self):
        with tempfile.TemporaryDirectory(prefix="upd-fail-") as d:
            checker(d, FakeFetcher(error=urllib.error.URLError("down"))).check(manual=True)
            state = wb_settings.update_check_state(d)
        self.assertGreater(state["last_attempt"], 0)
        self.assertEqual(state["last_success"], 0.0)
        self.assertEqual(state["latest_version"], "")


class QuotaFallbackTests(unittest.TestCase):
    """API 的匿名配额是每出口 IP 每小时 60 次，共享出口的额度可能被别人用光，
    之后从这个出口发起的检查都会失败。这里跑真实 fetcher、只替身 socket 层：
    别的套件注入的 fetcher 在回退逻辑之上，看不到回退。"""

    def _stub(self, web, api_error=None):
        """urlopen 替身：API 拒绝，网页端点返回 `web`。"""
        calls = []

        def urlopen(request, timeout=None):
            calls.append(request)
            if request.full_url == wb_updates.RELEASES_URL:
                raise api_error or urllib.error.HTTPError(
                    request.full_url, 403, "rate limited", {}, None)
            if isinstance(web, Exception):
                raise web
            return web

        return urlopen, calls

    def test_an_api_quota_error_falls_back_to_the_web_endpoint(self):
        with tempfile.TemporaryDirectory(prefix="upd-quota-") as d:
            urlopen, calls = self._stub(Redirected(tag_url("v1.6.20")))
            with mock.patch.object(wb_updates.urllib.request, "urlopen", urlopen):
                status = checker(d, wb_updates.fetch_releases).check(manual=True)
            self.assertEqual(len(calls), 2, "the API goes first, the web endpoint second")
            self.assertEqual(status["last_error"], "")
            self.assertEqual(status["latest_version"], "1.6.20")
            self.assertTrue(status["update_available"])
            self.assertEqual(status["release_url"], tag_url("v1.6.20"))

    def test_a_transport_error_on_the_api_path_also_falls_back(self):
        # 代理可能掐掉一个主机、放行另一个，所以回退不关心 API 调用失败的原因。
        with tempfile.TemporaryDirectory(prefix="upd-quota-") as d:
            urlopen, _calls = self._stub(Redirected(tag_url("v1.6.20")),
                                         api_error=urllib.error.URLError("reset"))
            with mock.patch.object(wb_updates.urllib.request, "urlopen", urlopen):
                status = checker(d, wb_updates.fetch_releases).check(manual=True)
            self.assertEqual(status["last_error"], "")
            self.assertEqual(status["latest_version"], "1.6.20")

    def test_a_failing_fallback_still_reports_the_api_cause(self):
        with tempfile.TemporaryDirectory(prefix="upd-quota-") as d:
            urlopen, _calls = self._stub(urllib.error.URLError("no route"))
            with mock.patch.object(wb_updates.urllib.request, "urlopen", urlopen):
                status = checker(d, wb_updates.fetch_releases).check(manual=True)
            self.assertEqual(status["last_error"], "HTTP 403")
            self.assertIsNone(status["latest_version"])

    def test_the_optional_token_is_sent_and_never_stored(self):
        token = "ghp-not-a-real-token"
        sent = []

        def urlopen(request, timeout=None):
            sent.append(request.get_header("Authorization"))
            return io.BytesIO(json.dumps([release("v1.6.20")]).encode("utf-8"))

        with tempfile.TemporaryDirectory(prefix="upd-quota-") as d:
            with mock.patch.dict(os.environ, {wb_updates.TOKEN_ENV: token}):
                with mock.patch.object(wb_updates.urllib.request, "urlopen", urlopen):
                    status = checker(d, wb_updates.fetch_releases).check(manual=True)
            self.assertEqual(sent[0], "Bearer " + token)
            self.assertEqual(status["latest_version"], "1.6.20")
            with open(wb_settings.settings_path(d), encoding="utf-8") as fh:
                raw = fh.read()
        self.assertNotIn(token, raw)


class BackgroundWorkerTests(unittest.TestCase):
    def test_the_worker_is_a_daemon_and_stops_promptly(self):
        with tempfile.TemporaryDirectory(prefix="upd-bg-") as d:
            worker = checker(d, FakeFetcher([release("v1.6.18")]),
                             first_delay=3600, poll_seconds=3600)
            self.assertTrue(worker.daemon, "a non-daemon thread would hold the process open")
            worker.start()
            worker.stop()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def test_a_disabled_worker_never_fetches(self):
        with tempfile.TemporaryDirectory(prefix="upd-bg-") as d:
            fetcher = FakeFetcher([release("v1.6.18")])
            worker = checker(d, fetcher)
            worker.start()
            time.sleep(0.2)
            worker.stop()
            worker.join(timeout=5)
        self.assertEqual(fetcher.calls, [], "the switch is off: no background request")

    def test_an_enabled_worker_checks_on_its_own(self):
        with tempfile.TemporaryDirectory(prefix="upd-bg-") as d:
            wb_settings.set_update_check_enabled(d, True)
            fetcher = FakeFetcher([release("v1.6.18")])
            worker = checker(d, fetcher)
            worker.start()
            deadline = time.time() + 5
            while not fetcher.calls and time.time() < deadline:
                time.sleep(0.02)
            worker.stop()
            worker.join(timeout=5)
        self.assertGreaterEqual(len(fetcher.calls), 1)
        self.assertFalse(worker.is_alive())

    def test_an_enabled_worker_checks_before_the_first_delay(self):
        """开关打开时启动即查一次：首个延迟只约束之后的轮询。"""
        with tempfile.TemporaryDirectory(prefix="upd-bg-") as d:
            wb_settings.set_update_check_enabled(d, True)
            fetcher = FakeFetcher([release("v1.6.18")])
            worker = checker(d, fetcher, first_delay=3600, poll_seconds=3600)
            worker.start()
            deadline = time.time() + 5
            while not fetcher.calls and time.time() < deadline:
                time.sleep(0.02)
            worker.stop()
            worker.join(timeout=5)
        self.assertEqual(len(fetcher.calls), 1, "启动时就该查一次，不等首个延迟")
        self.assertFalse(worker.is_alive())


class RouteTests(unittest.TestCase):
    class Request(object):
        _handle_settings_save = proxy.Handler._handle_settings_save
        _get_updates = proxy.Handler._get_updates
        _route_update_check = proxy.Handler._route_update_check

        def __init__(self, payload=None):
            self.payload = payload
            self.status = None
            self.reply = None

        def _payload_or_error(self, allow_list=False):
            return self.payload

        def _authorized(self):
            return True

        def _panel_ok(self):
            return True

        def _error(self, status, message, kind=""):
            self.status = status
            self.reply = message
            return status, message

        def _json(self, status, payload):
            self.status = status
            self.reply = payload
            return status, payload

    def _save(self, directory, payload, updates=None):
        request = self.Request(payload)
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, UPDATES=updates):
            request._handle_settings_save()
        return request

    def test_settings_save_toggles_the_switch(self):
        with tempfile.TemporaryDirectory(prefix="upd-route-") as d:
            request = self._save(d, {"update_check_enabled": True})
            self.assertEqual(request.status, 200)
            self.assertIs(request.reply["update_check_enabled"], True)
            self.assertTrue(wb_settings.update_check_enabled(d))
            request = self._save(d, {"update_check_enabled": False})
            self.assertFalse(wb_settings.update_check_enabled(d))

    def test_settings_save_refuses_a_non_boolean(self):
        with tempfile.TemporaryDirectory(prefix="upd-route-") as d:
            for value in ("false", 0, 1, None, "yes"):
                request = self._save(d, {"update_check_enabled": value})
                self.assertEqual(request.status, 400, value)
            self.assertFalse(wb_settings.update_check_enabled(d))

    def test_turning_it_on_wakes_a_parked_worker(self):
        with tempfile.TemporaryDirectory(prefix="upd-route-") as d:
            worker = checker(d, FakeFetcher([release("v1.6.18")]),
                             first_delay=3600, poll_seconds=3600)
            worker.start()
            try:
                self._save(d, {"update_check_enabled": True}, updates=worker)
                self.assertTrue(worker._wake.is_set())
            finally:
                worker.stop()
                worker.join(timeout=5)

    def test_the_status_route_reports_without_checking(self):
        with tempfile.TemporaryDirectory(prefix="upd-route-") as d:
            fetcher = FakeFetcher([release("v1.6.18")])
            worker = checker(d, fetcher)
            with mock.patch.object(proxy, "UPDATES", worker):
                request = self.Request()
                request._get_updates()
            self.assertEqual(request.status, 200)
            # 路由报的是 worker 的 current_version（生产里 worker 就是用
            # running_version() 构造的，这里被换成了用 CURRENT 构造的桩），
            # 所以跟 CURRENT 比。跟源码版本比会在每次发版时必挂——CURRENT 是
            # 给下面 pick_latest 那组比较用例用的固定基准，不随发版变。
            self.assertEqual(request.reply["current_version"], CURRENT)
            self.assertFalse(request.reply["update_available"])
            self.assertEqual(fetcher.calls, [], "a status read must not spend a request")

    def test_the_manual_route_runs_a_check_and_answers_with_it(self):
        with tempfile.TemporaryDirectory(prefix="upd-route-") as d:
            fetcher = FakeFetcher([release("v1.6.18")])
            worker = checker(d, fetcher)
            with mock.patch.object(proxy, "UPDATES", worker):
                request = self.Request()
                request._route_update_check()
            self.assertEqual(request.status, 200)
            self.assertIs(request.reply["ok"], True)
            self.assertEqual(request.reply["latest_version"], "1.6.18")
            self.assertTrue(request.reply["update_available"])
            self.assertEqual(len(fetcher.calls), 1)

    def test_the_status_route_still_answers_without_a_worker(self):
        with tempfile.TemporaryDirectory(prefix="upd-route-") as d:
            with mock.patch.multiple(proxy, UPDATES=None, ACCOUNTS_DIR=d):
                request = self.Request()
                request._get_updates()
            self.assertEqual(request.status, 200)
            self.assertFalse(request.reply["update_available"])
            self.assertEqual(request.reply["msg"], "更新检查未运行")


class DispatchRequest(proxy.Handler):
    """由内存套接字驱动的真实 Handler。

    更新状态属于管理状态，拒绝发生在哪一层是分发器的属性而不是端点方法的
    属性，所以请求必须走真实的 do_GET/do_POST，测试才有意义。
    """

    def __init__(self, path, headers=None, command="GET"):
        self.path = path
        self.command = command
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        raw = "".join("%s: %s\r\n" % item for item in (headers or {}).items())
        self.headers = email.message_from_string(raw)
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass

    def dispatch(self):
        """跑真实分发器并解码它写出的应答。"""
        getattr(self, "do_" + self.command)()
        raw = self.wfile.getvalue()
        return self.status, (json.loads(raw.decode("utf-8")) if raw else None)


class UpdateRouteAccessTests(unittest.TestCase):
    """`/updates` 属于管理状态：要面板会话，只有 API Key 不够。"""

    KEY = "panel-secret-key"

    def _accounts(self, directory, with_key):
        if with_key:
            wb_settings.set_api_keys(directory, [
                {"id": "k1", "name": "k", "key": self.KEY, "enabled": True}])

    def _dispatch(self, directory, path, headers=None, command="GET",
                  with_key=False, updates=None):
        self._accounts(directory, with_key)
        if updates is None:
            updates = checker(directory, FakeFetcher([release("v1.6.18")]))
            updates.check(manual=True)   # 先跑一次，状态里才有答案可报
        request = DispatchRequest(path, headers=headers, command=command)
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, UPDATES=updates,
                                 API_KEY=None):
            return request.dispatch()

    def test_the_route_is_on_the_panel_boundary(self):
        self.assertTrue(proxy.Handler._is_panel_route("/updates"))
        self.assertTrue(proxy.Handler._is_panel_route("/updates/check"))

    def test_an_anonymous_request_cannot_read_the_status(self):
        with tempfile.TemporaryDirectory(prefix="upd-auth-") as d:
            status, body = self._dispatch(d, "/updates")
        self.assertEqual(status, 401)
        self.assertIn("panel password required", body["error"]["message"])
        self.assertNotIn("current_version", body)

    def test_an_api_key_alone_cannot_read_the_status(self):
        with tempfile.TemporaryDirectory(prefix="upd-auth-") as d:
            status, body = self._dispatch(
                d, "/updates",
                headers={"Authorization": "Bearer " + self.KEY}, with_key=True)
        self.assertEqual(status, 401)
        self.assertIn("panel password required", body["error"]["message"])
        self.assertNotIn("current_version", body)

    def test_a_valid_panel_session_can_read_the_status(self):
        with tempfile.TemporaryDirectory(prefix="upd-auth-") as d:
            token = proxy.PANEL.create()
            status, body = self._dispatch(
                d, "/updates", headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        self.assertEqual(body["current_version"], CURRENT)
        self.assertTrue(body["update_available"])

    def test_an_invalid_panel_token_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="upd-auth-") as d:
            status, _body = self._dispatch(
                d, "/updates", headers={"X-Panel-Token": "not-a-session"})
        self.assertEqual(status, 401)

    def test_the_manual_check_is_panel_only_too(self):
        with tempfile.TemporaryDirectory(prefix="upd-auth-") as d:
            status, _body = self._dispatch(
                d, "/updates/check", command="POST",
                headers={"Authorization": "Bearer " + self.KEY}, with_key=True)
            self.assertEqual(status, 401)
            token = proxy.PANEL.create()
            status, body = self._dispatch(
                d, "/updates/check", command="POST",
                headers={"X-Panel-Token": token})
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
