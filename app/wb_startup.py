"""启动装配：命令行、实例探测、运行态初始化与监听。网关模块对象由入口以 gateway
参数显式传入，运行态全局都写在它上面；本模块不反向查找网关。
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from http.server import ThreadingHTTPServer

import wb_accounts
import wb_activity
import wb_logstore
import wb_pricing
import wb_scheduler
import wb_settings
import wb_updates
import wb_upstream_pool
import wb_usage
from wb_models import VIRTUAL_ALIAS_MODELS, seed_default_disabled, start_catalog_daily
from wb_translate import local_ip_addresses


def main(gateway):
    args = _parse_cli_args(gateway)
    gateway._LISTEN_PORT = args.port
    _apply_cli_overrides(gateway, args)
    if _probe_running_instance(gateway, args):
        return
    api_key_generated = _bootstrap_runtime(gateway, args)
    if _report_first_run(gateway, args):
        return
    _log_startup_summary(gateway, args, api_key_generated)
    _serve_forever(gateway, args)

def _parse_cli_args(gateway):
    ap = argparse.ArgumentParser(description="WorkBuddy (workbuddy.ai) -> OpenAI-compatible proxy")
    ap.add_argument("--info", help="path to the WorkBuddy *.info credential file")
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or "8788"))
    ap.add_argument("--lan", action="store_true",
                    help="listen on every interface so other devices on the LAN can "
                         "reach it (implies --host 0.0.0.0 and forces an api key)")
    ap.add_argument("--api-key", default=os.environ.get("API_KEY") or os.environ.get("WB_PROXY_KEY") or None,
                    help="require this bearer token on /v1/* (optional)")
    ap.add_argument("--system-prompt", default=gateway.DEFAULT_SYSTEM_PROMPT,
                    help="system message injected when the request has none (required upstream)")
    ap.add_argument("--user-agent", default=None,
                    help="override the upstream User-Agent (default: mirror the official "
                         "WorkBuddy AI client)")
    ap.add_argument("--usage-dir", default=None,
                    help="where to store usage.jsonl (default: ./usage)")
    ap.add_argument("--accounts-dir", default=os.environ.get("ACCOUNTS_DIR") or None,
                    help="where the per-account credential files live (default: ./accounts)")
    ap.add_argument("--import-desktop", action="store_true",
                    help="import the desktop app credential as an account, then exit")
    ap.add_argument("--panel-password", default=None,
                    help="set the web panel password on startup (default: admin)")
    args = ap.parse_args()
    return args

def _apply_cli_overrides(gateway, args):
    # LAN 模式绑定所有网卡。API Key 在后面 ACCOUNTS_DIR 定下来之后才生成，
    # 以便持久化到设置里供下次复用。
    if args.lan and args.host == "127.0.0.1":
        args.host = "0.0.0.0"
    if args.user_agent:
        wb_accounts.USER_AGENT = args.user_agent.strip()
        gateway.log("user-agent override : %s" % wb_accounts.USER_AGENT)
    if args.usage_dir:
        gateway.USAGE_DIR = os.path.abspath(args.usage_dir)
        gateway.USAGE_LOG = os.path.join(gateway.USAGE_DIR, "usage.jsonl")
        # 事件日志（remaining 预算样本的来源）必须跟着 --usage-dir 走：常量在 import
        # 时按默认目录算过，漏掉它就一直写默认目录、报 ENOENT。
        gateway.LIMIT_EVENTS_FILE = os.path.join(gateway.USAGE_DIR, "limit-events.jsonl")
        # 按天归档的估算数据同理（少了它，跨天归档会一直写默认目录或报 ENOENT）。
        gateway.ESTIMATE_DAILY_FILE = os.path.join(gateway.USAGE_DIR, "estimate-daily.json")
        # recent 的行偏移索引同理：漏掉它换目录后索引写到旧目录，新目录每次冷启动都要重扫。
        gateway.RECENT_INDEX_FILE = os.path.join(gateway.USAGE_DIR, "usage-recent-index.json")
    # 账号活动历史与用量日志同目录，并且要在任何后台线程起来之前定下来：调度器
    # 的第一轮巡检不能落在 --usage-dir 生效之前，否则记录会写进默认目录。
    wb_activity.set_data_dir(gateway.USAGE_DIR)

def _probe_running_instance(gateway, args):
    # 拒绝启动第二份。Windows 上 SO_REUSEADDR 允许两个 socket 绑定同一端口，
    # 入站连接会被静默分流到两边，极难排查。
    try:
        probe = urllib.request.urlopen(
            f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/health",
            timeout=2,
        )
        existing = json.loads(probe.read().decode("utf-8"))
    except Exception:
        existing = None  # /health 无人应答，交给下面的绑定判定
    if isinstance(existing, dict):
        # 只有自己的 /health 带账号池字段 "accounts"。同端口的其他服务（开发代理、
        # 另一个网关）也会用 JSON 应答 /health；当作已启动会静默退出，看板显示的是
        # 对方的界面，API 调用全部 401/404。
        foreign = existing.get("service") or "accounts" not in existing
        if foreign:
            who = existing.get("service") or "an unknown HTTP service"
            print()
            print(f"  [ERROR] port {args.port} is already taken by another program: {who}")
            print("          wb-proxy itself is NOT running - nothing was started.")
            print()
            print("  Fix: start wb-proxy on a different port, e.g.")
            print("          %s" % gateway.launcher_hint(args.port + 1))
            print(f"          python3 wb_proxy.py --port {args.port + 1}")
            print()
            print("  Check who owns the port:  %s" % gateway.port_owner_hint(args.port))
            print()
            raise SystemExit(1)
        print()
        print(f"  [已有一个反向代理在 {args.port} 端口运行，无需重复启动]")
        print(f"  账号: {existing.get('uid', '?')} @ {existing.get('domain', '?')}")
        print(f"  看板: http://127.0.0.1:{args.port}/")
        print()
        print("  如果要重启: 先把原来那个窗口关掉（或结束 python 进程），再运行本程序。")
        print()
        # 必须返回 True 让 main() 收住。裸 return 得到 None，main() 会当作
        # 「没有运行中的实例」继续绑定已被占用的端口。
        return True
    return False

def _resolve_startup_api_key(args, accounts_dir, panel_keys):
    """启动参数、局域网生成与面板 Key 列表的优先级；列表存在时返回空。"""
    if panel_keys:
        return None, False, False
    api_key = args.api_key
    generated = False
    if args.lan and not api_key:
        api_key, generated = wb_settings.ensure_launcher_key(accounts_dir)
    saved_key, key_from_panel = wb_settings.api_key_override(accounts_dir)
    if key_from_panel and not args.api_key:
        return saved_key, False, True
    return api_key, generated, False

def _bootstrap_runtime(gateway, args):
    api_key_generated = False
    gateway.SYSTEM_PROMPT = args.system_prompt
    if args.accounts_dir:
        gateway.ACCOUNTS_DIR = os.path.abspath(args.accounts_dir)
    # 日志写入数据库（默认关闭）：在账号池之前初始化，好让「池加载失败」这类最需要
    # 事后留档的启动日志也能进库。关闭时只造一个 enabled=False 的空壳。
    gateway.LOG_STORE = wb_logstore.LogStore(os.path.join(gateway.USAGE_DIR, "logs.sqlite3"))
    gateway.apply_log_db_settings()
    # 面板 Key 列表存在时，列表是唯一凭据：启动参数与局域网自动生成都不参与。
    panel_keys = gateway.configured_keys()
    # 第三个返回值表示这把 Key 来自面板保存的设置文件；它决定 settings 视图
    # 把当前 Key 显示成面板 Key 还是启动参数 Key，必须写进网关状态。
    gateway.API_KEY, api_key_generated, gateway.API_KEY_FILE_SET = _resolve_startup_api_key(
        args, gateway.ACCOUNTS_DIR, panel_keys)
    if args.panel_password:
        wb_settings.set_panel_password(gateway.ACCOUNTS_DIR, args.panel_password)
        gateway.log("panel      : password set from --panel-password")
    elif wb_settings.panel_password_is_default(gateway.ACCOUNTS_DIR):
        gateway.log("panel      : password is still the default 'admin' - change it in the panel")
    gateway.POOL = wb_accounts.AccountPool(gateway.ACCOUNTS_DIR, log=gateway.log)
    gateway.POOL.load()
    gateway.POOL.apply_proxy_slots()
    gateway.POOL.apply_reserve_credits()
    gateway.POOL.apply_smart_routing()
    wb_usage.apply_routing_inputs(refresh=True)
    # WB_MAX_CONCURRENT_CHAT=auto：按刚加载完的账号池确定并发上限。此刻监听器
    # 还没启动，没有请求能占住名额，信号量替换是安全的。
    gateway.resize_chat_slots(gateway.POOL.count_ready())
    wb_usage.apply_daily_guards()
    # 虚拟档位别名默认停止处理；操作者在模型页勾选后，excluded_seen 保证
    # 下一次启动不会又关掉。
    seed_default_disabled(sorted(VIRTUAL_ALIAS_MODELS))
    wb_usage.load_persisted_realm()
    from wb_scheduler import Scheduler
    gateway.SCHEDULER = Scheduler(gateway.POOL)
    gateway.SCHEDULER.enabled = wb_settings.scheduler_enabled(gateway.ACCOUNTS_DIR)
    gateway.SCHEDULER.start()
    # 目录兜底：每天至少从上游 API 取一次（活跃使用的部署里 300s 缓存早就
    # 刷得更新，这个线程只兜长时间没人访问 / 一直吃缓存文件或内置快照）。
    start_catalog_daily()
    # 策略表与它的时间线跟用量日志放在同一目录：一个卷同时装下两者，
    # 请求里的引用就地解析。
    wb_pricing.set_data_dir(gateway.USAGE_DIR)
    # 面板的计价开关（总开关、变体继承）写在 settings.json；计价侧指向面板
    # 写入的同一个文件。
    wb_pricing.set_settings_dir(gateway.ACCOUNTS_DIR)
    wb_upstream_pool.set_settings_dir(gateway.ACCOUNTS_DIR)
    # 刷新器始终存在，/pricing 才能报告它的状态；总开关关闭时它的循环只是
    # 停着，不发抓取请求。
    gateway.PRICING = wb_pricing.PriceRefresher(
        wb_settings.pricing_refresh_minutes(gateway.ACCOUNTS_DIR))
    gateway.PRICING.start()
    # 余额支撑「临期额度优先派发」的调度偏好。刷新器在后台保持余额常新，
    # 请求路径不花查询代价。
    gateway.CREDITS_REFRESHER = wb_accounts.CreditsRefresher(gateway.POOL)
    gateway.CREDITS_REFRESHER.start()
    # 只做版本发现：检查器向 GitHub 查询最新稳定版并报告，不下载、不替换、
    # 不重启任何东西；每日开关默认关闭，关闭时不发出任何请求。
    gateway.UPDATES = wb_updates.UpdateChecker(
        current_version=gateway.running_version(),
        settings_dir=gateway.ACCOUNTS_DIR,
        log=lambda msg: gateway.add_log_entry("[更新] %s" % msg, tag="update"),
    )
    gateway.UPDATES.start()
    return api_key_generated

def startup_user_agents(accounts):
    """按有账号的区域返回官方桌面客户端的对话 UA，顺序为国内版、国际版。"""
    realms = {getattr(account, "realm", "") for account in accounts or ()}
    return [(realm, wb_accounts.REALM_CONFIGS[realm]["chat_ua"])
            for realm in ("cn", "intl") if realm in realms]

def _report_first_run(gateway, args):
    if args.info:
        account = gateway.POOL.import_desktop_credential(args.info, source="file")
        gateway.log("imported account %s from %s" % (account.uid[:8], args.info))
    if args.import_desktop:
        imported = wb_accounts.import_desktop_accounts()
        if not imported:
            gateway.log("no desktop credentials found on this machine")
        for account in imported:
            print("  %s  %s  %s" % (account.uid[:8], account.nickname, account.domain))
        return True
    first_run = not gateway.POOL.accounts
    if first_run:
        # 绝不静默采纳桌面客户端的登录：只报告扫到什么，让用户从看板导入。
        detected = wb_accounts.desktop_credential_scan()
        usable = [d for d in detected if d.get("valid")]
        if usable:
            gateway.log("no accounts yet - detected %d desktop credential(s), NOT importing" % len(usable))
            for d in usable:
                gateway.log("  available: %s  %s  %s" % (
                    (d.get("uid") or "?")[:8], d.get("nickname") or "(no name)",
                    d.get("realmName") or d.get("realm")))
            gateway.log("open the dashboard and click [Scan desktop app] to import")
        else:
            gateway.log("no accounts yet - no desktop credentials found on this machine")
    if first_run and not gateway.POOL.accounts:
        # 这里不能退出：看板必须保持可达，才能走浏览器登录流程添加新账号。
        gateway.log("still no accounts - starting anyway so you can log in via the dashboard")

def _log_startup_summary(gateway, args, api_key_generated):
    rep = gateway.current_account()
    gateway.log("accounts   : %d total, %d usable" % (len(gateway.POOL.accounts), gateway.POOL.count_ready()))
    for account in gateway.POOL.accounts:
        gateway.log("  - %s  %s  %s  %s" % (account.uid[:8], account.nickname or "(no name)",
                                    account.domain, wb_accounts._human_delta(
                                        (account.expires_at or 0) - time.time()) or "?"))
    gateway.log("store      : %s" % gateway.ACCOUNTS_DIR)
    gateway.log(f"credential : {rep.path if rep else chr(45)}")
    if os.path.exists(gateway.PRODUCT_CONFIG_CACHE):
        gateway.log(f"catalog    : {gateway.PRODUCT_CONFIG_CACHE}")
    else:
        gateway.log("catalog    : app cache not found - will use the model API instead")
    gateway.log(f"account    : {rep.uid if rep else chr(45)} @ {rep.domain if rep else chr(45)}")
    gateway.log(f"issuer     : {wb_accounts.jwt_issuer(rep.access_token) if rep else chr(45)}")
    gateway.log("realm      : %s (%s)" % (
        gateway.CURRENT_REALM,
        "www.workbuddy.ai" if gateway.CURRENT_REALM == "intl" else "www.workbuddy.cn"))
    panel_keys = gateway.configured_keys()
    if panel_keys and not gateway.API_KEY:
        if args.api_key:
            gateway.log("api key    : 面板已有 %d 个 Key，启动参数中的 Key 已忽略" % len(panel_keys))
        else:
            gateway.log("api key    : 面板中已有 %d 个 Key" % len(panel_keys))
    # 只列该区域官方桌面客户端的对话 UA；VS Code / CLI 身份按账号另算。
    for realm, ua in startup_user_agents(gateway.POOL.accounts):
        gateway.log("user-agent : %-4s %s" % (realm, ua))
    if args.host == "0.0.0.0":
        ips = local_ip_addresses() or ["<this-pc-ip>"]
        print()
        print("  " + "=" * 62)
        print("  LAN MODE - reachable from other devices")
        print()
        for ip in ips:
            print("    API       : http://%s:%s/v1" % (ip, args.port))
            print("    Dashboard : http://%s:%s/" % (ip, args.port))
        print()
        if gateway.API_KEY:
            print("    API Key   : %s" % gateway.API_KEY)
            if api_key_generated:
                print("                (newly generated & saved to accounts/settings.json)")
            else:
                print("                (reused from accounts/settings.json)")
            print()
            print("    Open the dashboard (key already included):")
            print("      http://%s:%s/?key=%s" % (ips[0], args.port, gateway.API_KEY))
            print()
            print("    Clients: Base URL = the API address above, then paste the key.")
            print()
        if gateway.IS_WINDOWS:
            print("    If nothing can connect, allow python through the")
            print("    firewall: run allow-firewall.bat once as administrator.")
        elif sys.platform == "darwin":
            print("    If other devices cannot connect, allow incoming")
            print("    connections for Python (macOS asks automatically the")
            print("    first time it listens; on macOS 15+ also allow Local")
            print("    Network access for your terminal). Helper script:")
            print("    ./allow-firewall.command")
        else:
            print("    If other devices cannot connect, open the port in")
            print("    your firewall (ufw / firewalld) for the LAN subnet.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()
    if not gateway.POOL.accounts:
        print()
        print("  " + "=" * 62)
        print("  NO ACCOUNTS YET")
        print()
        print("  Open the dashboard and click [Login new account]:")
        print("      http://127.0.0.1:%s/" % args.port)
        print()
        print("  The browser flow adds the account automatically.")
        print("  This window must stay open.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()

class GatewayServer(ThreadingHTTPServer):
    """主服务的 HTTP 服务类：只为把 listen backlog 从 stdlib 默认的 5 提到 128。
    backlog=5 在突发并发下（面板一页约 7 个并发请求）会让内核丢 SYN、客户端按 1 s
    粒度重传，部分请求慢 1 秒；listen() 在 TCPServer.__init__ 里就被调用，backlog
    必须是类属性。"""
    request_queue_size = 128

def _serve_forever(gateway, args):
    try:
        server = GatewayServer((args.host, args.port), gateway.Handler)
    except OSError as exc:
        # 端口在上面的探测与这次绑定之间被抢走，或被不答 /health 的程序占着：
        # 用大白话报告，不要直接抛出 socketserver 的原始回溯。
        print()
        print(f"  [ERROR] failed to listen on {args.host}:{args.port} - {exc}")
        print("          the port is reserved or held by another program;")
        print("          wb-proxy did NOT start.")
        print()
        print("  Fix: stop the program holding the port, or pick another port:")
        print("          %s" % gateway.port_owner_hint(args.port))
        print("          %s" % gateway.launcher_hint(args.port + 1))
        print()
        raise SystemExit(1)
    # 只有 socket 真正建立后才宣称在监听，绑定失败时不会打出与下面错误矛盾的行。
    # 日志报告请求路径实际执行的状态：面板可在启动后开启 Key 校验，只读 API_KEY
    # 会打出 "off"，而 /v1 全部被 401 拒绝。
    if gateway.auth_required():
        _panel_keys = [k for k in gateway.configured_keys() if k.get("enabled")]
        _key_state = ("on (%d key(s) from the panel)" % len(_panel_keys)) if _panel_keys else "on (--api-key)"
    else:
        _key_state = "off"
    gateway.log(f"listening  : http://{args.host}:{args.port}/v1  (api key: {_key_state})")
    gateway.log(f"dashboard  : http://{args.host}:{args.port}/")
    # handler 必须在进程存活期内保持引用：SetConsoleCtrlHandler 存的是裸指针，
    # 回调被回收后关闭控制台时会崩溃。
    _ctrl_handler = gateway.install_console_close_handler()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        gateway.log("bye")
    finally:
        try:
            server.server_close()
        except Exception:
            pass
