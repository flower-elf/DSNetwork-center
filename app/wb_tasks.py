import functools
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

import wb_accounts as _accounts
import wb_identity

CHAT_BASE = "https://copilot.tencent.com"
BILL_BASE = "https://www.codebuddy.cn"
WEB_BASE = "https://www.workbuddy.cn"
#: 本模块只跑国内版，桌面端 UA 与 wb_identity 共用同一来源，避免两处版本号不一致。
DESKTOP_UA = wb_identity.DESKTOP_UA_CN
WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"


_log = lambda msg: None


def set_logger(fn):
    """把任务诊断输出接到调用方提供的日志函数。

    成长接口会吞掉自己的失败，避免单个端点挂掉中断整轮；没有日志时这些失败
    完全不可见，上游改版与「今天没有任务」看起来一模一样。
    """
    global _log
    previous = _log
    _log = fn or (lambda msg: None)
    return previous

def _has_personal_program(account):
    """账号是否适用签到/成长/活跃这套个人体系；测试替身默认适用。"""
    return bool(getattr(account, "has_personal_program", lambda: True)())


_gates_lock = threading.Lock()
_gates = {}


def _growth_gate(uid):
    """取该账号的可重入闸门；同一线程内的嵌套调用（如全任务里跑猫猫旅行）直接放行。"""
    with _gates_lock:
        lock = _gates.get(uid)
        if lock is None:
            lock = threading.RLock()
            _gates[uid] = lock
        return lock


def acquire_growth(uid):
    """尝试占用账号闸门；成功返回锁，已被占用返回 None。成功后必须 release_growth。

    账号级活跃上报这类不在本模块的方法也要加同一把锁，避免与成长任务并发写进度。
    """
    lock = _growth_gate(uid or "")
    return lock if lock.acquire(blocking=False) else None


def release_growth(lock):
    lock.release()


def growth_guard(busy_msg):
    """把会修改成长状态的入口按账号串行化，重复触发时直接返回忙碌结果。

    面板、调度器与各任务入口都可能同时操作同一账号，重复上报事件或重复领奖
    没有意义，还会放大上游风控风险。
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(account, *args, **kwargs):
            lock = acquire_growth(getattr(account, "uid", "") or "")
            if lock is None:
                return {"ok": False, "busy": True, "msg": busy_msg,
                        "logs": ["⏭ " + busy_msg], "earned_credit": 0}
            try:
                return fn(account, *args, **kwargs)
            finally:
                release_growth(lock)
        return wrapper
    return decorate


TASK_SPECS = {
    "create_canvas": {"kind": "canvas", "target": 1, "reward": 300, "name": "创建设计任务"},
    "template_5": {"kind": "template", "target": 5, "reward": 200, "name": "模板创建任务"},
    "expert_5": {"kind": "expert", "target": 5, "reward": 200, "name": "使用专家助手"},
    "Expert_team_use_3": {"kind": "team", "target": 3, "reward": 150, "name": "使用专家团队"},
    "skill_1": {"kind": "skill", "target": 1, "reward": 100, "name": "体验技能"},
    "automation_1": {"kind": "automation", "target": 1, "reward": 100, "name": "创建自动化任务"},
    "playbook_prompt": {"kind": "playbook", "target": 1, "reward": 100, "name": "灵感案例使用"},
    "Expert_lighthouse": {"kind": "lighthouse", "target": 1, "reward": 100, "name": "轻量云专家使用"},
    "Buddy_App": {"kind": "buddy5", "target": 1, "reward": 100, "name": "进入 Buddy 应用"},
    "Hp_Appearance": {"kind": "skin", "target": 1, "reward": 100, "name": "应用主题外观"},
    "chat_5": {"kind": "chat", "target": 5, "reward": 100, "name": "发起 5 次对话"},
    "black_cat": {"kind": "cat", "night": True, "target": 3, "reward": 100,
                  "name": "夜猫子任务 (23:00-08:00)"},
    "RichMeow_Chat": {"kind": "richmeow", "target": 1, "reward": 100, "name": "桌面对话事件链"},
    "Library_read": {"kind": "library", "target": 1, "reward": 100, "name": "浏览资料库"},
    # first_buddy 是 auto 类任务，上游不接受接取；完成并领奖后才发放 Buddy
    # 实例，它是所有 single 任务与猫猫旅行的前置，缺它时上游回
    # "prerequisite not met: first_buddy"。
    "first_buddy": {"kind": "chat", "target": 1, "reward": 300, "name": "领取一只 Buddy"},
    # 活动任务：在专家页召唤「无畏契约拼豆专家」并完成一次对话。事件里的 id
    # 必须是这个专家的真实 id，否则上游只接受事件但不推进进度。
    "WorkBuddy_Valorant": {"kind": "expert", "target": 1, "reward": 100,
                           "expert": ("ex_xQHHD1u5Jkua", "无畏契约拼豆专家"),
                           "name": "体验「无畏契约拼豆专家」"},
    # 专家体验任务各 50 积分，专家 id 取自任务跳转链接；事件必须用真实 id。
    "expert_use_maidehao": {"kind": "expert", "target": 1, "reward": 50,
                            "expert": ("ex_GDl31WahyIH0", "中国电商运营专家"),
                            "name": "体验「中国电商运营专家」"},
    "expert_use_douyinyin": {"kind": "expert", "target": 1, "reward": 50,
                             "expert": ("ex_FbY5nlWTqDod", "抖音策略师"),
                             "name": "体验「抖音策略师」"},
    "expert_use_shangjia_xuanchuan": {"kind": "expert", "target": 1, "reward": 50,
                                      "expert": ("ex_Weo3x2EZuSDm",
                                                 "商家宣传页与海报设计师"),
                                      "name": "体验「商家宣传页与海报设计师」"},
    "expert_use_bowangwang": {"kind": "expert", "target": 1, "reward": 50,
                              "expert": ("ex_WZY136wV5aMt", "直播电商教练"),
                              "name": "体验「直播电商教练」"},
    "wb_wechat_oa_subscribe_task": {"unforgeable": True, "reward": 100,
                                    "reason": "需在微信关注「腾讯 WorkBuddy」公众号满 24 小时",
                                    "name": "关注官方公众号"},
}

# ---------------------------------------------------------------------------
# 桌面端行为常量，2026-09 实测校准
# ---------------------------------------------------------------------------
# 这些任务上游只认桌面客户端的真实行为信号，jump_url 均为 workbuddy:// 深链，
# 需要真实点击进入对应页面。伪造 /v2/report 事件会被忽略或落到 heartbeat，
# 进度停在 0/1，claim 返回 400 "task not completed"，因此跳过并给出深链。
DESKTOP_ONLY_TASKS = {
    "RichMeow_Chat": "在桌面端发起 1 次对话",
    "Library_read": "在桌面端打开「资料库」并读完介绍文档",
    "Buddy_App": "在桌面端左上角「发现应用」进入任意一个 Buddy 应用",
}

# 夜猫子任务只在 23:00-08:00 上报才计数，且每天 1 次、累计 3 天。
# 上游可能短期下线该任务；重新上线后按同一套代码自动恢复，无需改代码。
NIGHT_TASK_CODES = {"black_cat"}


def is_night_task(t):
    """判断一条上游任务是否属于夜猫子类。

    代码匹配之外，允许 spec 用 night=True 显式标注改名后的夜猫任务；不按通用
    事件 kind 推断，避免把其他 cat 类任务当夜猫任务接取与领奖。
    """
    code = t.get("task_code") or ""
    if code in NIGHT_TASK_CODES:
        return True
    return bool((TASK_SPECS.get(code) or {}).get("night"))


def automation_for(t):
    """任务能否自动推进：返回 ("auto", "") 或 ("manual", 原因)。

    未登记事件的任务、只能桌面端真实操作的任务与不可伪造的任务都归为手动；
    接取不受影响，只是本轮不上报事件。
    """
    code = t.get("task_code") or ""
    spec = TASK_SPECS.get(code)
    if not spec:
        return "manual", "暂未登记自动化"
    if spec.get("unforgeable"):
        return "manual", spec.get("reason") or "需真实操作"
    if code in DESKTOP_ONLY_TASKS:
        return "manual", DESKTOP_ONLY_TASKS[code]
    return "auto", ""


def cst_now():
    """UTC+8 墙上时间；夜猫子窗口与调度器都按这个时区算，不受机器时区影响。"""
    return time.gmtime(time.time() + 8 * 3600)


def in_night_window():
    h = cst_now().tm_hour
    return h >= 23 or h < 8

# 专家/团队事件必须彼此不同：桌面端 appendGrowthEvent 按 eventCode 与 id 去重，
# 上游同样只按不同 id 累加进度，重复发同一个 id 进度不变。
# 以下 id 已在 2026-09 实测中验证可推进任务进度。
EXPERT_ID_POOL = [
    ("ex_PZw8Gu81HfN4", "运维工程师"), ("ex_ROsDtJbzADFV", "产品经理"),
    ("ex_SMUnl0nJbPix", "UI设计师"), ("ex_ZTR062oVBOCW", "数据分析师"),
    ("ex_a3sSSFBy8qaC", "后端架构师"), ("ex_aG1kvKbq8lPx", "文案策划"),
    ("ex_al1vxtUOYQ10", "测试专家"), ("ex_cZfiyuET9UQP", "安全顾问"),
    ("ex_eggOvQuVP0hq", "算法工程师"), ("ex_hSwsQjkSKnkX", "前端工程师"),
    ("ex_mMbwwmFA9n9P", "项目管理专家"), ("ex_uAQE5POfk7Zh", "增长运营专家"),
    ("ex_uZzSAScSy7FZ", "行业研究员"), ("ex_LHywGrZOtG7G", "数据分析师"),
    ("ex_NX5C8GBciVed", "测试架构师"), ("ex_DdCsaoq4AtcO", "云端运维专家"),
    ("ex_KzqKQguubrNQ", "内容创作专家"), ("ex_2cvvUZQhDyeJ", "腾讯轻量云专家"),
]
# 团队 id 来自官方专家清单 expert_center.json，expertType=team，共 53 个；
# 前 3 个已在 2026-09 实测验证可推进 Expert_team_use_3。
TEAM_ID_POOL = [
    ("CloudOpsTeam", "运维专家团队"), ("CloudContentTeam", "内容专家团队"),
    ("CloudDevTeam", "研发专家团队"), ("ProductStrategyTeam", "产品战略团队"),
    ("MarketingCampaignTeam", "营销活动团队"), ("SalesBattleTeam", "销售作战团队"),
    ("DesignEngineTeam", "设计引擎团队"), ("HrOperationsTeam", "人力运营团队"),
]


def query_growth_tasks(account):
    """查询成长任务列表，返回 (是否成功, 任务列表)。

    失败与「上游没有任务」必须区分：网络或应答异常时 ok 为 False，调用方据此
    保留失败状态，不会把故障当成任务下线。
    """
    if not _has_personal_program(account):
        return True, []
    url = CHAT_BASE + "/v2/activity/growth/tasks"
    req = urllib.request.Request(url, headers=account.headers("chat"))
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            # 与其它成长接口一致：带 code 字段时 0 才算成功。上游把业务错误
            # （登录失效、活动下线）放在 200 的正文里，不能当成空任务列表。
            if "code" in data and data.get("code") != 0:
                _log("growth tasks rejected: code=%s msg=%s"
                     % (data.get("code"), data.get("msg")))
                return False, []
            raw_tasks = (data.get("data") or {}).get("tasks") or []
            tasks = []
            for t in raw_tasks:
                code = t.get("task_code") or ""
                spec = TASK_SPECS.get(code, {})
                prog = t.get("progress") or {}
                automation, manual_reason = automation_for({"task_code": code})
                tasks.append({
                    "task_code": code,
                    "name": t.get("title") or spec.get("name") or code,
                    "description": t.get("description") or t.get("task_desc") or "",
                    "jump_url": t.get("jump_url") or "",
                    "status": t.get("accept_status") or "not_accepted",
                    "task_type": t.get("task_type") or "",
                    "current": prog.get("current", 0),
                    "target": prog.get("target", spec.get("target", 1)),
                    "reward_credit": t.get("reward_credit") or spec.get("reward", 0),
                    "reward_energy": t.get("reward_energy", 0),
                    "unforgeable": bool(spec.get("unforgeable")),
                    "reason": spec.get("reason", ""),
                    "manual": automation == "manual",
                    "manual_reason": manual_reason,
                })
            return True, tasks
    except Exception as exc:
        _log("growth tasks query failed: %s (%s)" % (url, exc))
        return False, []


def fetch_growth_tasks(account):
    """查询成长任务列表；失败时返回空列表，供只关心内容的调用方使用。"""
    return query_growth_tasks(account)[1]


def fetch_growth_summary(account):
    """查询连续打卡、猫猫旅行与能量余额。"""
    out = {"energy": 0, "streak_days": 0, "travel": {"state": "unknown"}}
    if not _has_personal_program(account):
        return out
    headers = account.headers("chat")
    # 1. 能量
    try:
        req = urllib.request.Request(CHAT_BASE + "/v2/activity/growth/energy", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["energy"] = (d.get("data") or {}).get("balance", 0)
    except Exception as exc:
        _log(f"growth/energy query failed: {exc}")
    # 2. 连续打卡、连登档位、补签卡与抽奖
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/streak", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            data = d.get("data") or {}
            st = data.get("streak") or {}
            out["streak_days"] = st.get("days", 0)
            out["makeup_cards"] = (data.get("makeup_cards") or {}).get("balance", 0)
            redemp = data.get("redemption_status") or {}
            out["tiers"] = redemp.get("tiers") or []
            out["tier_statuses"] = {
                "7d": redemp.get("tier_7d_status", "locked"),
                "14d": redemp.get("tier_14d_status", "locked"),
                "28d": redemp.get("tier_28d_status", "locked"),
            }
    except Exception as exc:
        _log(f"growth/streak query failed: {exc}")
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/lottery/summary", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["lottery_chances"] = (d.get("data") or {}).get("chances", 0)
    except Exception as exc:
        _log(f"growth/lottery query failed: {exc}")
    # 3. 猫猫旅行
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/status", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["travel"] = d.get("data") or {}
    except Exception as exc:
        _log(f"buddy/travel/status query failed: {exc}")
    return out


def fetch_streak_days(account):
    """只读查询账号当前连续打卡天数，接口为 GET /activity/growth/streak。

    失败返回 None，不发写请求。供对话活跃上报后回显连登天数使用。
    """
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/streak",
                                     headers=account.headers("chat"))
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            st = (d.get("data") or {}).get("streak") or {}
            return st.get("days")
    except Exception as exc:
        _log(f"fetch_streak_days failed: {exc}")
        return None

def accept_tasks(account, codes, chunk=20):
    """批量接取任务。

    上游按批返回 results，单个任务可能 status=accepted / already_accepted /
    其它失败原因。返回 {"ok": bool, "accepted": [...], "failed": [...],
    "msg": str}，上层据此判断是否值得继续上报事件：未接取任务上的上报不会
    计入进度，领奖时上游回 "task not completed"。
    """
    out = {"ok": True, "accepted": [], "failed": [], "msg": "", "reasons": []}
    if not codes:
        return out
    url = CHAT_BASE + "/v2/activity/growth/tasks/accept"
    for i in range(0, len(codes), max(1, chunk)):
        part = codes[i:i + max(1, chunk)]
        body = json.dumps({"task_codes": part}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers=account.headers("chat"))
        try:
            with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
                d = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(300).decode("utf-8", "replace")
            except Exception:
                pass
            out["ok"] = False
            out["msg"] = "HTTP %s: %s" % (exc.code, detail[:200])
            out["failed"].extend(part)
            out["reasons"].append(out["msg"])
            _log(f"accept batch failed: {out['msg']}")
            continue
        except Exception as exc:
            out["ok"] = False
            out["msg"] = str(exc)[:200]
            out["failed"].extend(part)
            _log(f"accept batch failed: {exc}")
            continue
        if d.get("code") != 0:
            out["ok"] = False
            out["msg"] = d.get("msg") or ("code=%s" % d.get("code"))
            out["failed"].extend(part)
            out["reasons"].append(out["msg"])
            _log(f"accept rejected: {out['msg']}")
            continue
        results = (d.get("data") or {}).get("results") or []
        seen = set()
        for item in results:
            code = item.get("task_code")
            st = str(item.get("status") or "")
            seen.add(code)
            if st in ("accepted", "already_accepted"):
                out["accepted"].append(code)
            else:
                out["failed"].append(code)
                out["reasons"].append("%s: %s" % (code, item.get("message") or st))
                _log(f"task {code} accept status={st}")
        for code in part:
            if code not in seen:
                out["failed"].append(code)
    return out


def claim_task(account, code):
    """领取任务奖励。支持 copilot.tencent.com -> www.workbuddy.cn 自动降级。"""
    url = f"{CHAT_BASE}/activity/growth/tasks/{code}/claim"
    req = urllib.request.Request(url, data=b"{}", method="POST", headers=account.headers("chat"))
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            if d.get("code") == 0:
                data = d.get("data") or {}
                return {"ok": True, "credit": data.get("credit", 0), "energy": data.get("energy", 0)}
            # 200 响应但 code 非 0，典型是进度还没落账就来领奖，
            # 上游回 task not completed。
            _log(f"task {code} claim rejected: {d.get('msg')}")
            return {"ok": False, "credit": 0, "energy": 0, "msg": d.get("msg") or f"code={d.get('code')}"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        err_msg = f"HTTP {exc.code}"
        try:
            err_msg = json.loads(body).get("msg") or err_msg
        except Exception:
            pass
        if exc.code == 400:
            # 降级到 web 域领奖
            web_url = f"{WEB_BASE}/activity/growth/tasks/{code}/claim"
            web_hdrs = {
                "Authorization": "Bearer " + account.access_token,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": WEB_BASE,
                "Referer": f"{WEB_BASE}/profile/growth-center",
                "x-client-platform": "web",
                "User-Agent": WEB_UA,
                "X-User-Id": account.uid,
                "X-Domain": WEB_BASE,
            }
            try:
                req_web = urllib.request.Request(web_url, data=b"{}", method="POST", headers=web_hdrs)
                with _accounts.urlopen(req_web, timeout=15, proxy=account.proxy) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    if d.get("code") == 0:
                        data = d.get("data") or {}
                        return {"ok": True, "credit": data.get("credit", 0), "energy": data.get("energy", 0)}
                    _log(f"task {code} web claim rejected: code={d.get('code')} msg={d.get('msg')}")
                    return {"ok": False, "credit": 0, "energy": 0, "msg": d.get("msg") or err_msg}
            except Exception as exc2:
                _log(f"task {code} web claim failed: {exc2}")
        _log(f"task {code} claim failed: {err_msg}")
        return {"ok": False, "credit": 0, "energy": 0, "msg": err_msg}
    except Exception as exc:
        _log(f"task {code} claim failed: {exc}")
    return {"ok": False, "credit": 0, "energy": 0}


def build_event(account, kind, idx=0, expert=None):
    """构造指定类型的真实规范事件数据。

    expert 是 expert_id 与 expert_name 组成的元组。专家/团队类事件必须每次
    使用不同的 id，上游按 eventCode 与 id 去重，重复 id 不会推进任务进度。
    """
    now = int(time.time() * 1000)
    cid = f"wb-task-{now}-{idx}"
    rid = f"{cid}-req"
    uid = account.uid

    if kind == "canvas":
        return {"eventCode": "wbx_design_canvas_task_create", "timestamp": now,
                "reportDelay": 0, "conversationId": cid, "requestId": rid,
                "source": "summon_keyword", "isCustomModel": False, "name": "",
                "inputLength": 12, "id": f"wbx-canvas-{now}", "cost": 0,
                "isSuccessful": True, "userId": uid}
    if kind == "template":
        return {"eventCode": "agent_task_created_with_template", "timestamp": now,
                "reportDelay": 0, "isCustomModel": True, "id": str(idx),
                "name": "幻灯片", "requestId": rid, "conversationId": cid, "userId": uid}
    if kind in ("expert", "team", "lighthouse"):
        etype = "team" if kind == "team" else "agent"
        if expert:
            ex_id, name = expert
        elif kind == "lighthouse":
            ex_id, name = "ex_2cvvUZQhDyeJ", "腾讯轻量云专家"
        elif kind == "team":
            ex_id, name = "CloudOpsTeam", "运维专家团队"
        else:
            ex_id, name = "ContentCreator", "内容创作专家"
        return {"eventCode": "expert_actual_use", "timestamp": now, "reportDelay": 0,
                "mode": "CLOUD", "id": ex_id, "name": name, "expertTitle": name,
                "type": "02-Engineering", "expertType": etype, "source": "builtin",
                "version": "1.0.2", "cost": 0, "characterCount": 12, "conversationId": cid,
                "requestId": rid, "messageId": rid, "requestModelId": "deepseek-v4-flash",
                "requestModelName": "DeepSeek V4 Flash", "userId": uid}
    if kind == "skill":
        return {"eventCode": "skill_info", "timestamp": now, "reportDelay": 0,
                "skillId": "skill_2096525080079265792", "name": "pptx", "userId": uid}
    if kind == "automation":
        return {"eventCode": "automated_task_create_suc", "timestamp": now, "reportDelay": 0,
                "name": "每周工作整理", "type": "cron", "source": "manually",
                "modelId": "deepseek-v4-flash", "modelIsThinking": False,
                "conversationId": cid, "requestId": rid,
                "schedule": {"type": "recurring", "rrule": "FREQ=WEEKLY;BYDAY=FR;BYHOUR=9;BYMINUTE=0"},
                "prompt": "每周五自动整理本周工作", "userId": uid}
    if kind == "playbook":
        return {"eventCode": "playbook_prompt_send", "timestamp": now, "reportDelay": 0,
                "id": "worker-ledger-freedom-dashboard", "name": "打工人小账本",
                "type": "other", "promptLength": 10, "isOfficial": 1,
                "source": "discover", "conversationId": cid, "requestId": rid, "userId": uid}
    if kind == "skin":
        return {"eventCode": "appearance_skin_apply", "timestamp": now, "reportDelay": 0,
                "action": "apply", "source": "settings_close", "id": "theme-tkmw7j",
                "vipLevel": "free", "series": "craft", "type": "unknown",
                "name": "和平精英激战金秋", "userId": uid}
    if kind in ("chat", "cat"):
        m_id = "glm-5.2" if kind == "cat" else "deepseek-v4-flash"
        m_nm = "GLM-5.2" if kind == "cat" else "DeepSeek V4 Flash"
        mode = "night" if kind == "cat" else "craft"
        return {"eventCode": "chat_request_send", "timestamp": now, "reportDelay": 0,
                "mode": mode, "conversationId": cid, "requestId": rid,
                "inputLength": 12, "requestModelId": m_id, "requestModelName": m_nm,
                "isPlan": False, "agentName": "default", "agentType": "conversation",
                "userId": uid}
    return {"eventCode": "heartbeat", "timestamp": now, "userId": uid}


def report_events(account, events, base=None):
    """向上游上报事件数组。

    默认发往 copilot.tencent.com 的 chat 侧，与桌面客户端的真实上报地址
    一致，即 POST https://copilot.tencent.com/v2/report；2026-09 实测该侧
    事件会实时推进成长任务进度。
    """
    if base is None:
        base = CHAT_BASE
    url = base + "/v2/report"
    headers = account.headers("billing" if base == BILL_BASE else "chat")
    body = json.dumps(events).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            return d.get("code") == 0
    except Exception:
        return False


def _travel_status(account):
    req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/status",
                                 headers=account.headers("chat"))
    with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
        d = json.loads(resp.read().decode("utf-8"))
        return d.get("data") or {}


def ensure_buddy(account, gap=1.0):
    """确保账号已领到 Buddy 实例：没有则完成并领取 first_buddy。

    first_buddy 是 auto 类任务，其奖励字段 reward_buddy 就是 Buddy 实例；
    single 任务与猫猫旅行都以它为前提。返回是否已就绪。
    """
    tasks = fetch_growth_tasks(account)
    fb = next((t for t in tasks if t["task_code"] == "first_buddy"), None)
    if fb is None:
        return False
    if fb["status"] == "claimed":
        return True
    _progress_one(account, fb, [], gap)
    tasks = fetch_growth_tasks(account)
    fb = next((t for t in tasks if t["task_code"] == "first_buddy"), None)
    return bool(fb and fb["status"] == "claimed")


def _travel_depart(account, lid):
    """派出旅行；返回 data 与 err 两个值。成功时 data 为响应 data，失败时 err 为上游 msg。"""
    body = json.dumps({"location_id": lid}).encode("utf-8")
    req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/depart",
                                 data=body, method="POST", headers=account.headers("chat"))
    try:
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if res.get("code") == 0:
            return res.get("data") or {}, None
        return None, res.get("msg") or ("code=%s" % res.get("code"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        try:
            msg = json.loads(detail).get("msg") or ("HTTP %s" % exc.code)
        except Exception:
            msg = "HTTP %s%s" % (exc.code, (": " + detail[:120]) if detail else "")
        return None, msg
    except Exception as exc:
        return None, str(exc)


@growth_guard("该账号的猫猫旅行正在执行，请稍后再试")
def do_cat_travel(account):
    """检查并执行猫猫旅行，含领奖与派出。"""
    if not _has_personal_program(account):
        return {"ok": True, "action": "skip",
                "msg": "企业版或静态 API Key 账号没有成长体系，跳过"}
    headers = account.headers("chat")
    # 1. 查询状态
    try:
        st = _travel_status(account)
    except Exception as exc:
        return {"ok": False, "msg": f"查询旅行状态失败: {exc}"}

    state = st.get("state")
    if state == "arrived":
        # 领奖，官方前端为 POST travel/claim，body 是 {}。
        req_cl = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/claim", data=b"{}", method="POST", headers=headers)
        try:
            with _accounts.urlopen(req_cl, timeout=10, proxy=account.proxy) as resp:
                c_res = json.loads(resp.read().decode("utf-8"))
                credit = (c_res.get("data") or {}).get("reward_credit", 0)
                account.fetch_credits()
                return {"ok": True, "action": "claim", "credit": credit, "msg": f"旅行归来领奖成功！获得 {credit} 积分"}
        except Exception as e:
            return {"ok": False, "msg": f"领奖失败: {e}"}

    if state == "idle":
        if st.get("daily_limit_reached"):
            return {"ok": True, "action": "idle", "msg": "猫猫今日已完成旅行，明日 00:00 刷新"}
        # 派出旅行：官方前端要求 body 携带 location_id，目的地清单在
        # travel/config；空 body 会被拒 400 "invalid request"。
        lid = None
        try:
            req_cfg = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/config", headers=headers)
            with _accounts.urlopen(req_cfg, timeout=10, proxy=account.proxy) as resp:
                cfg = json.loads(resp.read().decode("utf-8"))
                locs = (cfg.get("data") or {}).get("locations") or []
            if locs:
                lid = locs[0].get("id")
        except Exception as exc:
            _log(f"buddy/travel/config query failed: {exc}")
        if lid is None:
            lid = 1
        # 状态里的 buddy_id 是当前旅行实例的 id，猫在家即 idle 时恒为 0，
        # 不能拿它判断有没有 Buddy。以 depart 的 "no active buddy" 为准：
        # 先直接派，失败再补齐 first_buddy 并重试一次，其奖励即 Buddy 实例。
        data, err = _travel_depart(account, lid)
        if data is None and "no active buddy" in (err or "").lower():
            if ensure_buddy(account):
                data, err = _travel_depart(account, lid)
            else:
                return {"ok": False,
                        "msg": "还没有 Buddy：需先完成并领取「领取一只 Buddy」任务（新建任务并发起对话）"}
        if data is not None:
            loc = (data.get("location") or {}).get("name") or ""
            return {"ok": True, "action": "depart", "msg": f"猫猫已出发前往「{loc}」，预计数小时后归来！"}
        return {"ok": False, "msg": f"派出旅行失败: {err}"}

    if state == "traveling":
        return {"ok": True, "action": "traveling", "msg": "猫猫正在旅行途中，请稍后再来查看！"}

    return {"ok": True, "action": state, "msg": f"当前状态: {state}"}

def _pre_report(account, t, logs, gap):
    """上报前的分类与即时结算。

    返回 (action, credit)：
    - ("done", 积分)   已领奖或已终结，不需要再上报。
    - ("skip", 0)      本轮跳过（桌面端真实任务、夜间窗口外、未接取、不可伪造）。
    - ("report", 0)    需要上报事件点亮，交给调用方上报并结算。
    """
    code = t["task_code"]
    spec = TASK_SPECS.get(code) or {}
    name = spec.get("name") or t.get("name") or code
    status = t["status"]
    cur = t.get("current", 0)
    tgt = t.get("target", 1)

    if status == "claimed":
        return "done", 0

    # 已完成的任务先领奖：桌面端/夜间任务也可能被真实操作完成，例如用户
    # 自己在桌面端用了一次，或夜里调度器点亮了。这类任务必须先结算，
    # 不能因为只能靠真实操作就直接跳过丢掉奖励。未登记事件的任务同样适用。
    if status == "completed" or cur >= tgt:
        res = claim_task(account, code)
        if res.get("ok"):
            cr = res.get("credit", 0)
            logs.append(f"✓ 任务 [{name}] 领奖成功: +{cr} 积分")
            time.sleep(gap)
            return "done", cr
        logs.append(f"! 任务 [{name}] 领奖失败: {res.get('msg') or '未知原因'}")
        time.sleep(gap)
        return "done", 0

    # 只分两类：可自动推进，或必须手动操作。手动任务照常接取，本轮不上报。
    action, reason = automation_for(t)
    if action == "manual":
        jump = t.get("jump_url") or ""
        suffix = f" (深链 {jump})" if jump else ""
        logs.append(f"⏭ 任务 [{name}] 需手动操作完成: {reason}{suffix}, 跳过事件上报")
        return "skip", 0

    # 夜猫子任务只在 23:00-08:00 计数，每日 01:00 调度器也会自动执行。
    if is_night_task(t) and not in_night_window():
        logs.append(f"🌙 任务 [{name}] 仅 23:00-08:00 上报计数, 当前不在窗口, 跳过"
                    f" (每日 01:00 调度器自动执行, 累计 3 天)")
        return "skip", 0

    if status == "not_accepted" and t.get("task_type") != "auto":
        # 接取没成功就上报没有效果，上游只对已接取的任务累计进度。
        logs.append(f"⏭ 任务 [{name}] 仍未接取, 跳过 (先解决接取失败)")
        return "skip", 0

    return "report", 0


def _emit_events(account, t, logs, gap):
    """为需要点亮的任务上报补足进度所需的事件。

    专家/团队事件必须使用互不相同的 id，否则上游按 eventCode 与 id 去重，
    进度不会变化。
    """
    code = t["task_code"]
    spec = TASK_SPECS.get(code) or {}
    cur = t.get("current", 0)
    tgt = t.get("target", 1)
    need = max(1, tgt - cur)
    kind = spec.get("kind")
    logs.append(f"正在点亮任务 [{spec.get('name') or code}] (需上报 {need} 次)...")
    report_ok = True
    id_pool = None
    if kind in ("expert", "team"):
        id_pool = TEAM_ID_POOL if kind == "team" else EXPERT_ID_POOL
    fixed_expert = spec.get("expert")
    for i in range(need):
        expert = fixed_expert
        if expert is None and id_pool:
            pid, pnm = id_pool[(cur + i) % len(id_pool)]
            expert = (pid, pnm)
        ev = build_event(account, kind, idx=i, expert=expert)
        if not report_events(account, [ev]):
            report_ok = False
        if i < need - 1:
            time.sleep(gap)
    if not report_ok:
        logs.append(f"! 任务 [{spec.get('name') or code}] 部分事件上报失败 (上游拒绝), 继续尝试领奖")


def _settle_reported(account, pending, logs, gap):
    """结算一批刚上报过的任务：共享轮询整份任务列表确认进度，达标则领奖。

    pending 为 task_code 到 (上报前进度, 目标, 名称) 的映射，目标取自账号自己
    返回的任务对象。整批只拉取任务列表，拉取次数与任务数无关，取代每个任务
    各自拉满 6 次。进度通常 1-3 秒落账，一旦某任务进度超过上报前基线，就视为
    在动并继续等到上限，避免异步累计任务过早报未达成。
    """
    earned = 0
    if not pending:
        return earned
    time.sleep(1.5)
    remaining = dict(pending)
    for attempt in range(6):
        snapshot = {x["task_code"]: x for x in fetch_growth_tasks(account)}
        advancing = False
        for code in list(remaining):
            base, tgt, name = remaining[code]
            fresh = snapshot.get(code)
            if not fresh:
                continue
            if fresh.get("status") == "claimed":
                # 刷新后已是已领状态（例如桌面端或上一轮已领），直接移出，不重复领奖。
                del remaining[code]
                continue
            prog = fresh.get("current", 0)
            if prog >= tgt or fresh.get("status") == "completed":
                res = claim_task(account, code)
                if res.get("ok"):
                    cr = res.get("credit", 0)
                    earned += cr
                    logs.append(f"✓ 任务 [{name}] 点亮并领奖成功: +{cr} 积分")
                else:
                    logs.append(f"! 任务 [{name}] 进度已达 {prog}/{tgt} 但领奖失败: {res.get('msg') or '未知原因'}")
                del remaining[code]
                time.sleep(gap)
            elif prog > base:
                advancing = True
        if not remaining:
            break
        if advancing:
            # 有任务进度在涨，值得多等一会儿。
            time.sleep(2.5)
        elif attempt < 2:
            time.sleep(1.5)
        else:
            break
    for code, (base, tgt, name) in remaining.items():
        logs.append(f"? 任务 [{name}] 已上报但进度未达成 (目标 {tgt}), 领奖顺延到下次运行")
    return earned


def _progress_one(account, t, logs, gap):
    """推进单个任务并返回本次到账积分。

    供 ensure_buddy 等单任务场景使用：分类后若需点亮，上报并单独结算。
    """
    action, credit = _pre_report(account, t, logs, gap)
    if action != "report":
        return credit
    _emit_events(account, t, logs, gap)
    _name = (TASK_SPECS.get(t["task_code"]) or {}).get("name") or t.get("name") or t["task_code"]
    return _settle_reported(
        account, {t["task_code"]: (t.get("current", 0), t.get("target", 1), _name)},
        logs, gap)


@growth_guard("该账号的成长任务正在执行，请稍后再试")
def run_growth_tasks(account, gap=1.0):
    """完整执行批量成长任务点亮与领奖。"""
    if account.realm != "cn":
        return {"ok": False, "msg": "成长任务中心仅支持国内版账号", "logs": []}
    if not _has_personal_program(account):
        return {"ok": True, "msg": "企业版或静态 API Key 账号没有成长体系，跳过",
                "logs": []}
    logs = []
    logs.append(f"开始为账号 {account.nickname or account.uid[:8]} 运行成长任务自动化")
    # 先取一次任务列表再执行：auto 前置、批量接取与逐任务点亮都基于这份列表。
    tasks = fetch_growth_tasks(account)
    if not tasks:
        logs.append("未能获取到任务清单，请检查网络或账号状态")
        return {"ok": False, "logs": logs, "earned_credit": 0}

    total_earned = 0

    # 0. auto 类任务如 first_buddy 不需要接取，且可能是其他任务与猫猫旅行的
    #    前置：必须先把它们点亮并领奖，first_buddy 的奖励就是 Buddy 实例，
    #    否则 single 任务接取会报 "prerequisite not met: first_buddy"。
    for t in tasks:
        if t.get("task_type") == "auto":
            total_earned += _progress_one(account, t, logs, gap)

    # 1. 批量接取未接任务：只有 auto 类不用接取。
    def _needs_accept(x):
        """所有未接取的任务一律接取。

        上游按账号决定给哪些任务，接取是后续上报与领奖的前提。事件暂时无法
        构造、只能真实操作或不可伪造的任务也照常接取，只在 `_pre_report` 里
        点名说明本轮未推进。
        """
        return (x["status"] == "not_accepted"
                and x.get("task_type") != "auto")

    unaccepted = [t["task_code"] for t in tasks if _needs_accept(t)]
    if unaccepted:
        logs.append(f"发现 {len(unaccepted)} 个待接取任务，正在批量接取")
        acc = accept_tasks(account, unaccepted)
        if acc.get("failed"):
            why = acc.get("msg") or ("; ".join(acc.get("reasons") or [])[:160])
            logs.append(f"! 接取未成功 {len(acc['failed'])} 个: {', '.join(acc['failed'][:5])}"
                        + (" ..." if len(acc["failed"]) > 5 else "")
                        + (f" ({why})" if why else ""))
        if acc.get("accepted"):
            logs.append(f"✓ 已接取 {len(acc['accepted'])} 个任务")
        time.sleep(gap)
        tasks = fetch_growth_tasks(account)
        # 复核一次：上游偶尔会瞬时拒绝整个批次，复查后仍处于未接取的再补一次，
        # 否则后面所有上报都作用在未接取的任务上，进度全是 0。
        still = [t["task_code"] for t in tasks if _needs_accept(t)]
        if still:
            logs.append(f"仍有 {len(still)} 个未接取，重试接取一次")
            retry = accept_tasks(account, still)
            if retry.get("accepted"):
                logs.append(f"✓ 重试接取成功 {len(retry['accepted'])} 个")
            time.sleep(gap)
            tasks = fetch_growth_tasks(account)
        if not tasks:
            logs.append("! 接取后无法获取任务清单，本轮中止")
            return {"ok": False, "logs": logs, "earned_credit": total_earned}
        still_pending = [t["task_code"] for t in tasks if _needs_accept(t)]
        if still_pending:
            logs.append(f"! 仍有 {len(still_pending)} 个任务处于未接取状态，"
                        f"对未接取任务上报事件不会计入进度，本轮跳过这些任务")

    # 2. 普通任务分两段：先逐个上报点亮，再共享轮询整份列表统一结算领奖。
    #    结算只按时间窗拉取任务列表，次数与任务数无关，避免每个任务各拉满 6 次。
    pending = {}
    for t in tasks:
        if t.get("task_type") == "auto":
            continue
        action, credit = _pre_report(account, t, logs, gap)
        total_earned += credit
        if action == "report":
            _emit_events(account, t, logs, gap)
            _name = (TASK_SPECS.get(t["task_code"]) or {}).get("name") or t.get("name") or t["task_code"]
            pending[t["task_code"]] = (t.get("current", 0), t.get("target", 1), _name)
            # 批量上报仍然保持任务之间的间隔，避免连续发出多个上报请求。
            time.sleep(gap)
    total_earned += _settle_reported(account, pending, logs, gap)

    # 3. 检查猫猫旅行。
    tr_res = do_cat_travel(account)
    if tr_res.get("msg"):
        logs.append(f"猫猫日常: {tr_res.get('msg')}")
        if tr_res.get("credit"):
            total_earned += tr_res.get("credit", 0)

    # 4. 刷新积分余额
    account.fetch_credits()
    credits = account.credits or {}
    logs.append(f"🎉 全部完成！本次累计新增到账: +{total_earned} 积分，当前总剩余 {credits.get('remain', 0)} 积分")
    return {"ok": True, "logs": logs, "earned_credit": total_earned, "credits": credits}


@growth_guard("该账号的夜猫子任务正在执行，请稍后再试")
def run_night_growth(account):
    """夜猫子任务 black_cat：每日 23:00-08:00 上报 1 次 GLM-5.2 夜间对话事件，
    每天计 1 次、累计 3 天后可领奖。白天调用会跳过。"""
    if account.realm != "cn":
        return {"ok": False, "msg": "成长任务中心仅支持国内版账号", "logs": [], "earned_credit": 0}
    if not _has_personal_program(account):
        return {"ok": True, "msg": "企业版或静态 API Key 账号没有成长体系，跳过",
                "logs": [], "earned_credit": 0}
    logs = []
    name = account.nickname or account.uid[:8]
    if not in_night_window():
        return {"ok": True, "earned_credit": 0, "logs": [
            f"[{name}] 当前不在 23:00-08:00 夜间窗口, 夜猫子任务跳过 (每日 01:00 自动执行)"]}
    ok_query, tasks = query_growth_tasks(account)
    if not ok_query:
        return {"ok": False, "earned_credit": 0, "logs": [
            f"[{name}] 夜猫子任务清单查询失败，今晚跳过"]}
    t = next((x for x in tasks if is_night_task(x)), None)
    if not t:
        # 查询成功但列表里没有这项：上游可能临时下线，回来时同一条链路自动恢复。
        return {"ok": True, "earned_credit": 0, "logs": [
            f"[{name}] 上游当前未提供夜猫子任务，今晚跳过"]}
    code = t["task_code"]
    if t["status"] == "claimed":
        return {"ok": True, "earned_credit": 0, "logs": [f"[{name}] 夜猫子任务已领奖"]}
    if t["status"] == "not_accepted":
        # 上游只对已接取的任务累计进度；夜猫子任务也必须先接取。
        acc_res = accept_tasks(account, [code])
        if not acc_res.get("accepted"):
            why = acc_res.get("msg") or ("; ".join(acc_res.get("reasons") or [])[:120])
            logs.append(f"[{name}] 夜猫子任务未接取, 今晚跳过 (先解决接取失败{': ' + why if why else ''})")
            return {"ok": False, "earned_credit": 0, "logs": logs}
        logs.append(f"[{name}] 夜猫子任务已接取")
    cur, tgt = t.get("current", 0), t.get("target", 3)
    if cur >= tgt:
        res = claim_task(account, code)
        cr = res.get("credit", 0) if res.get("ok") else 0
        account.fetch_credits()
        logs.append(f"[{name}] 夜猫子任务达标, 领奖 {'✓ +' + str(cr) + ' 积分' if res.get('ok') else '! 失败: ' + (res.get('msg') or '')}")
        return {"ok": res.get("ok", False), "earned_credit": cr, "logs": logs}
    ev = build_event(account, "cat", idx=0)
    ok = report_events(account, [ev])
    logs.append(f"[{name}] 上报夜间 GLM-5.2 对话事件: {'成功' if ok else '失败'} (进度 {cur}/{tgt})")
    fresh = None
    for _ in range(4):
        time.sleep(4)
        fresh = next((x for x in fetch_growth_tasks(account) if x["task_code"] == code), None)
        if fresh and fresh.get("current", 0) > cur:
            break
    new_cur = fresh.get("current", cur) if fresh else cur
    if new_cur >= tgt:
        res = claim_task(account, code)
        cr = res.get("credit", 0) if res.get("ok") else 0
        logs.append(f"[{name}] 夜猫子任务完成 {new_cur}/{tgt}, 领奖 {'✓ +' + str(cr) + ' 积分' if res.get('ok') else '! 失败: ' + (res.get('msg') or '')}")
    elif new_cur > cur:
        logs.append(f"[{name}] 今晚 +1 ({new_cur}/{tgt}), 明晚继续, 累计 3 天可领奖")
    else:
        logs.append(f"[{name}] 已上报但进度暂未变化 ({new_cur}/{tgt}), 明晚调度器会继续累计")
    account.fetch_credits()
    earned = 0
    for line in logs:
        m = re.search(r"\+(\d+) 积分", line)
        if m:
            earned = int(m.group(1))
    return {"ok": True, "earned_credit": earned, "logs": logs}

@growth_guard("该账号的连登管家正在执行，请稍后再试")
def run_streak_bonus(account):
    """连登管家：补签保连登、礼包与补偿、连登档位兑换 7d/14d/28d、自动抽奖闭环。

    幂等设计：未解锁、已领、无次数、企业版均安全静默跳过。
    """
    if account.realm != "cn":
        return {"ok": False, "msg": "连登管家仅限国内版账号"}
    if not _has_personal_program(account):
        return {"ok": True, "msg": "企业版或静态 API Key 账号没有成长体系，跳过"}

    headers = account.headers("chat")
    logs = []

    # 0. 检查昨日漏签并自动补签
    try:
        req_hm = urllib.request.Request(CHAT_BASE + "/activity/growth/heatmap", headers=headers)
        with _accounts.urlopen(req_hm, timeout=10, proxy=account.proxy) as resp:
            hm_data = json.loads(resp.read().decode("utf-8"))
            cells = (hm_data.get("data") or {}).get("cells", [])
            yesterday_str = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
            y_cell = next((c for c in cells if c.get("date") == yesterday_str), None)
            if y_cell and y_cell.get("score", 0) == 0:
                req_st = urllib.request.Request(CHAT_BASE + "/activity/growth/streak", headers=headers)
                with _accounts.urlopen(req_st, timeout=10, proxy=account.proxy) as st_resp:
                    st_data = json.loads(st_resp.read().decode("utf-8"))
                    cards = ((st_data.get("data") or {}).get("makeup_cards") or {}).get("balance", 0)
                    if cards > 0:
                        body = json.dumps({"target_date": yesterday_str}).encode("utf-8")
                        req_use = urllib.request.Request(CHAT_BASE + "/activity/growth/makeup-cards/use",
                                                         data=body, method="POST", headers=headers)
                        with _accounts.urlopen(req_use, timeout=10, proxy=account.proxy):
                            logs.append(f"已消耗 1 张补签卡补签昨日 ({yesterday_str})")
    except Exception as exc:
        _log(f"makeup yesterday failed: {exc}")

    # 1. 尝试领新手礼包与补偿，每号一次，无则业务错误静默。
    b_hdrs = account.headers("billing")
    for path, name in [("/billing/meter/claim-gift", "新手礼包"),
                       ("/billing/meter/claim-compensation", "活动补偿")]:
        try:
            req_g = urllib.request.Request(BILL_BASE + path, data=b"{}", method="POST", headers=b_hdrs)
            with _accounts.urlopen(req_g, timeout=10, proxy=account.proxy) as resp:
                d = json.loads(resp.read().decode("utf-8"))
                if d.get("code") == 0:
                    cr = (d.get("data") or {}).get("credit", 0)
                    logs.append(f"领取{name} +{cr}积分")
        except Exception:
            pass

    # 2. 检查 7d/14d/28d 连登档位兑换
    try:
        req_st = urllib.request.Request(CHAT_BASE + "/activity/growth/streak", headers=headers)
        with _accounts.urlopen(req_st, timeout=10, proxy=account.proxy) as resp:
            st_data = json.loads(resp.read().decode("utf-8"))
            redemp = (st_data.get("data") or {}).get("redemption_status") or {}
            tiers = redemp.get("tiers", [])
            statuses = {
                "7d": redemp.get("tier_7d_status"),
                "14d": redemp.get("tier_14d_status"),
                "28d": redemp.get("tier_28d_status"),
            }
            for t in tiers:
                tier_name = t.get("tier")
                status = statuses.get(tier_name)
                if status in ("locked", "claimed"):
                    continue
                body = json.dumps({"tier": tier_name, "client_token": str(uuid.uuid4())}).encode("utf-8")
                req_rd = urllib.request.Request(CHAT_BASE + "/activity/growth/redeem",
                                                data=body, method="POST", headers=headers)
                try:
                    with _accounts.urlopen(req_rd, timeout=10, proxy=account.proxy) as rd_resp:
                        rd_json = json.loads(rd_resp.read().decode("utf-8"))
                        if rd_json.get("code") == 0:
                            logs.append(f"成功兑换连登 {tier_name} 档位")
                except Exception as exc:
                    _log(f"redeem {tier_name} failed: {exc}")
    except Exception as exc:
        _log(f"streak redeem check failed: {exc}")

    # 3. 自动抽奖闭环，有 chances 则全部抽完。
    drawn_count = 0
    try:
        req_lot = urllib.request.Request(CHAT_BASE + "/activity/growth/lottery/summary", headers=headers)
        with _accounts.urlopen(req_lot, timeout=10, proxy=account.proxy) as resp:
            lot_data = json.loads(resp.read().decode("utf-8"))
            chances = (lot_data.get("data") or {}).get("chances", 0)
            for _ in range(chances):
                body = json.dumps({"client_token": str(uuid.uuid4())}).encode("utf-8")
                req_draw = urllib.request.Request(CHAT_BASE + "/activity/growth/lottery/draw",
                                                  data=body, method="POST", headers=headers)
                try:
                    with _accounts.urlopen(req_draw, timeout=10, proxy=account.proxy) as draw_resp:
                        d_res = json.loads(draw_resp.read().decode("utf-8"))
                        if d_res.get("code") == 0:
                            drawn_count += 1
                except Exception as exc:
                    _log(f"lottery draw failed: {exc}")
                    break
                time.sleep(1.0)
            if drawn_count > 0:
                logs.append(f"完成成长抽奖 {drawn_count} 次")
    except Exception as exc:
        _log(f"lottery summary failed: {exc}")

    msg = "; ".join(logs) if logs else "连登状态正常（无需兑换/无抽奖次数）"
    return {"ok": True, "logs": logs, "msg": msg}
