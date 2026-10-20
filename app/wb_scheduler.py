"""wb_scheduler.py —— 后台定时调度器 (Scheduler)

负责常驻后台自动执行：
1. Token 保活 (Keepalive)：定期检查 Token 剩余寿命，不足 2 小时自动调用 Refresh Token。
2. 每日签到 (Daily Checkin)：每日定时为所有国内版账号自动签到领积分。
3. 对话活跃上报 (Activity Report)：每日定时为国内版账号上报轻量会话事件，点亮连登与热力墙。
4. 猫猫旅行与日常结算 (Cat Travel & Welfare)：自动派出猫猫旅行或领取归来奖励。
5. 状态持久化与看板展示：暴露状态、执行记录、支持手动立即触发与开关切换。
"""
import sys
import threading
import time
import random
import wb_settings
import wb_tasks
from wb_tasks import do_cat_travel


class Scheduler:
    def __init__(self, pool):
        self.pool = pool
        # 时段来自设置；默认保持原有排程（09:00/21:00、22:00、01:00）。
        self.checkin_hours = []
        self.travel_hours = []
        self.keepalive_hours = []
        self.cat_hours = []
        self.all_hours = []
        self.enabled = True
        self._stop_event = threading.Event()
        self._thread = None
        self.last_run_time = None
        self.next_run_time = None
        self.logs = []
        self.apply_schedule()
        # Guards against overlapping runs: trigger_now() spawns a thread per
        # click, and a manual trigger can also land on top of the hourly job.
        self._run_lock = threading.Lock()
        # Surface task-level failures (dead endpoints, upstream shape changes)
        # in the same log the panel shows.
        wb_tasks.set_logger(self.log)

    def apply_schedule(self, hours=None):
        """按设置更新四组整点时段；不传时从账号目录读取。"""
        directory = getattr(self.pool, "dir", "")
        if hours is None:
            hours = (wb_settings.scheduler_hours(directory) if directory
                     else wb_settings.DEFAULT_SCHEDULER_HOURS)
        self.checkin_hours = list(hours.get("checkin") or [])
        self.travel_hours = list(hours.get("travel") or [])
        self.keepalive_hours = list(hours.get("keepalive") or [])
        self.cat_hours = list(hours.get("cat") or [])
        self.all_hours = sorted(set(self.checkin_hours + self.travel_hours
                                    + self.keepalive_hours + self.cat_hours))
        self._calc_next_fire()

    def log(self, msg, tag=None):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", wb_tasks.cst_now())
        entry = f"[{ts}] {msg}"
        self.logs.append(entry)
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]
        try:
            effective_tag = tag
            if not effective_tag:
                lower = str(msg).lower()
                if any(k in lower for k in ("签到", "打卡", "活跃", "猫猫", "连登", "夜猫", "抽奖", "兑换")):
                    effective_tag = "tasks"
                else:
                    effective_tag = "scheduler"
            # python app/wb_proxy.py makes the gateway __main__; importing
            # wb_proxy here would load a second copy with its own POOL.
            proxy_module = sys.modules.get("__main__")
            if proxy_module is None or not hasattr(proxy_module, "add_log_entry"):
                proxy_module = sys.modules.get("wb_proxy")
            if proxy_module is not None:
                proxy_module.add_log_entry(f"[调度器] {msg}", tag=effective_tag)
        except Exception:
            pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        event = threading.Event()
        self._stop_event = event
        self._thread = threading.Thread(target=self._run_loop, args=(event,),
                                        daemon=True)
        self._thread.start()
        self.log("后台定时调度器已启动")

    def stop(self):
        event = self._stop_event
        if event is not None:
            event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        if thread is None or not thread.is_alive():
            self._thread = None
        self.log("后台定时调度器已暂停")

    def _run_loop(self, stop_event):
        # 启动后先等待主服务就绪；stop() 要让这段等待立刻退出。
        if stop_event.wait(10):
            return
        try:
            self._execute_cycle("启动初次初始化巡检")
        except Exception as exc:
            self.log(f"初次巡检异常: {exc}")

        while not stop_event.is_set():
            self._calc_next_fire()
            # 每 30 秒检查一次当前整点
            now = wb_tasks.cst_now()
            cur_hour = now.tm_hour
            cur_min = now.tm_min
            if self.enabled:
                # 到达设定的整点前 1 分钟内触发
                if cur_min == 0 and cur_hour in self.all_hours:
                    reason = f"整点排程命中 ({cur_hour}:00)"
                    try:
                        self._execute_cycle(reason)
                    except Exception as exc:
                        self.log(f"排程执行异常: {exc}")
                    if stop_event.wait(65):
                        return
            if stop_event.wait(30):
                return

    def _calc_next_fire(self):
        now = wb_tasks.cst_now()
        cur_h = now.tm_hour
        next_h = None
        for h in self.all_hours:
            if h > cur_h or (h == cur_h and now.tm_min == 0 and now.tm_sec < 10):
                next_h = h
                break
        if next_h is not None:
            # 今天
            t_struct = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, next_h, 0, 0, 0, 0, -1))
        else:
            # 明天第一个小时（UTC+8 没有夏令时，epoch 直接加一天）
            now_tom = time.gmtime(time.time() + 8 * 3600 + 86400)
            first_h = self.all_hours[0]
            t_struct = time.struct_time((now_tom.tm_year, now_tom.tm_mon, now_tom.tm_mday, first_h, 0, 0, 0, 0, -1))
        self.next_run_time = time.strftime("%Y-%m-%d %H:%M:%S", t_struct)

    def trigger_now(self):
        """手动立即触发一次调度检查；强制跑完整轮，不受整点分组限制。"""
        if self._run_lock.locked():
            return {"ok": False, "msg": "已有巡检正在执行，请稍候再试"}
        threading.Thread(target=self._execute_cycle,
                         args=("手动立即触发", True), daemon=True).start()
        return {"ok": True, "msg": "已触发后台调度执行"}

    def _execute_cycle(self, trigger_reason="周期巡检", force=False):
        if not self._run_lock.acquire(blocking=False):
            self.log(f"跳过本次巡检 ({trigger_reason})：上一轮仍在执行")
            return
        try:
            self._run_cycle(trigger_reason, force)
        except Exception as exc:
            self.log(f"巡检异常 ({trigger_reason}): {exc}")
        finally:
            self._run_lock.release()

    def _run_cycle(self, trigger_reason="周期巡检", force=False):
        self.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S", wb_tasks.cst_now())
        proxy = sys.modules.get("wb_proxy") or sys.modules.get("__main__")
        if getattr(proxy, "stand_down_active", lambda: False)():
            self.log("账号被上游限制，网关已停止：跳过本次巡检")
            return
        self.log(f"开始执行任务 ({trigger_reason})...")
        if not self.pool or not self.pool.accounts:
            self.log("暂无可用的活跃账号，跳过本次巡检")
            return

        refreshed_count = 0
        checkin_count = 0
        report_count = 0
        travel_count = 0
        daily_chat_count = 0
        # 每个任务只在自己的配置整点执行：命中任意一个整点不再顺带跑完整轮。
        # 时间按 UTC+8 计算，与调度器其余部分一致；手动触发强制跑完整轮。
        cycle_hour = wb_tasks.cst_now().tm_hour
        run_checkin = force or cycle_hour in self.checkin_hours
        run_travel = force or cycle_hour in self.travel_hours
        run_keepalive = force or cycle_hour in self.keepalive_hours
        run_cat = force or cycle_hour in self.cat_hours

        processed = 0
        for idx, acc in enumerate(list(self.pool.accounts)):
            # 停用的账号一个请求都不发：签到、保活、活跃上报都算请求。
            if not acc.enabled:
                continue
            if processed:
                # 账号间随机错峰防并发风控；下限固定 2 秒，上限由设置页决定。
                directory = getattr(self.pool, "dir", "")
                upper = (wb_settings.task_jitter_max(directory) if directory
                         else wb_settings.TASK_JITTER_DEFAULT)
                time.sleep(random.uniform(wb_settings.TASK_JITTER_MIN, upper))
            processed += 1
            uid8 = acc.uid[:8] if acc.uid else "?"
            # 1. 检查 Token 剩余寿命 (小于 2 小时自动刷新保活)
            if run_keepalive:
                exp = acc.expires_at or 0
                if exp and (exp - time.time()) < 7200:
                    self.log(f"账号 [{uid8}] Token 即将到期，执行主动保活刷新")
                    if acc.refresh():
                        refreshed_count += 1
                        self.log(f"✓ 账号 [{uid8}] Token 自动保活刷新成功")
                    else:
                        self.log(f"! 账号 [{uid8}] Token 保活刷新失败: {acc.last_error}")

            # 2. 如果是国内版账号，检查每日签到与猫猫旅行
            if acc.realm == "cn":
                if run_checkin:
                    if acc.can_checkin():
                        self.log(f"检测到国内版账号 [{uid8}] 今日尚未签到，执行自动签到")
                        res = acc.checkin(trigger="scheduler")
                        if res.get("ok"):
                            checkin_count += 1
                            self.log(f"✓ 账号 [{uid8}] 自动签到成功: {res.get('msg')}")
                        else:
                            self.log(f"! 账号 [{uid8}] 自动签到未成功: {res.get('error') or res.get('msg')}")
                        time.sleep(1.0)

                    if acc.can_report_activity():
                        self.log(f"检测到国内版账号 [{uid8}] 今日尚未活跃上报，执行对话活跃上报")
                        res = acc.report_activity()
                        if res.get("ok"):
                            report_count += 1
                            self.log(f"✓ 账号 [{uid8}] {res.get('msg')}")
                        else:
                            self.log(f"! 账号 [{uid8}] 对话活跃上报失败: {res.get('error') or res.get('msg')}")
                        time.sleep(1.0)

                if run_travel:
                    # 检查猫猫旅行
                    tr = do_cat_travel(acc)
                    if tr.get("busy"):
                        self.log(f"🐱 账号 [{uid8}] 猫猫日常跳过: {tr.get('msg')}")
                    elif tr.get("action") in ("claim", "depart"):
                        travel_count += 1
                        self.log(f"🐱 账号 [{uid8}] 猫猫日常处理: {tr.get('msg')}")
                    time.sleep(1.0)

                    # 连登管家闭环（补签保连登 -> 礼包/补偿 -> 连登兑换 -> 自动抽奖）
                    bonus = wb_tasks.run_streak_bonus(acc)
                    if bonus.get("logs"):
                        for l in bonus["logs"]:
                            self.log(f"★ 账号 [{uid8}] {l}")
                        time.sleep(1.0)

                if run_cat:
                    # 夜猫子任务 black_cat 只在 23:00-08:00 上报计数，
                    # run_night_growth 自己会判断窗口。
                    night = wb_tasks.run_night_growth(acc)
                    for line in night.get("logs", []):
                        self.log(f"🌙 {line}")
                    time.sleep(1.0)

            # 3. 如果是国际版账号，检查每日活跃对话 (送 30/50 积分福利)
            if acc.realm == "intl" and run_checkin:
                if acc.can_daily_chat():
                    self.log(f"检测到国际版账号 [{uid8}] 今日尚未活跃，执行每日活跃打卡对话")
                    res = acc.daily_chat(trigger="scheduler")
                    if res.get("ok"):
                        daily_chat_count += 1
                        # 成功时也把网页通道那一轮的结果带上（completed：N 段输出），
                        # 光看「成功」分不清是真跑完了还是只有桌面腿过了（issue #236）。
                        self.log(f"✓ 账号 [{uid8}] 每日活跃对话成功: {res.get('msg')}")
                    else:
                        self.log(f"! 账号 [{uid8}] 每日活跃对话失败: {res.get('error') or res.get('msg')}")
                    time.sleep(1.5)

        self.log(f"巡检完成：Token保活 {refreshed_count} 个，国内签到 {checkin_count} 个，活跃上报 {report_count} 个，猫猫日常 {travel_count} 个，国际活跃 {daily_chat_count} 个")

    def status(self):
        def label(hours):
            return "/".join("%02d:00" % hour for hour in hours) if hours else "关闭"
        return {
            "enabled": self.enabled,
            "mode": "整点排程 (签到与活跃上报 %s · 猫猫旅行 %s · 保活 %s · 夜猫 %s)"
                    % (label(self.checkin_hours), label(self.travel_hours),
                       label(self.keepalive_hours), label(self.cat_hours)),
            "mode_cn": "整点排程 (签到与活跃上报 %s · 猫猫旅行 %s · 保活 %s · 夜猫 %s)"
                       % (label(self.checkin_hours), label(self.travel_hours),
                          label(self.keepalive_hours), label(self.cat_hours)),
            "mode_intl": "账号 Token 自动保活与凭证常驻 (每日 %s 集中巡检)"
                         % label(self.keepalive_hours),
            "last_run_time": self.last_run_time or "尚未运行",
            "next_run_time": self.next_run_time or "待调度",
            "logs": self.logs[-20:],
        }
