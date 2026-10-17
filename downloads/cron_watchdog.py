#!/usr/bin/env python3
"""Cron 任务完成情况巡查 Watchdog（2026-08-17 v2）

职责：检查 Hermes 全部定时任务是否【按时完成】：
  1. 调度器心跳：ticker_heartbeat 超过 15 分钟未更新 → 全局告警（调度器可能卡死）
  2. 失败检测：任一 enabled 任务 last_status=error/failed → 告警（附错误摘要）
  3. 漏跑检测：按 cron schedule 计算"上一个应运行点"，已过预期窗口
     但 last_run_at 缺失或早于该点 → 告警（该跑没跑）
  4. 静默：全部正常 → 空输出（no_agent watchdog 模式：空=不投递）

数据源：~/.hermes/cron/jobs.json（权威：last_run_at/last_status/last_error/next_run_at）
        ~/.hermes/cron/ticker_heartbeat（调度器心跳）

用法：python3 ~/.hermes/scripts/cron_watchdog.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

CRON_DIR = os.path.expanduser("~/.hermes/cron")
JOBS_FILE = os.path.join(CRON_DIR, "jobs.json")
HEARTBEAT_FILE = os.path.join(CRON_DIR, "ticker_heartbeat")

# 漏跑判定容差（分钟）：实际运行可比 schedule 晚，超过此值才算漏跑
GRACE_MIN = 90
# 调度器心跳超时（分钟）
HEARTBEAT_TIMEOUT_MIN = 15
# 错误摘要截断长度
ERR_TAIL = 160


def _parse_field(field, lo, hi):
    """解析单个 cron 字段，返回允许值集合；* 返回 None 表示全部。"""
    if field == "*":
        return None
    values = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            base = range(lo, hi + 1)
        elif "-" in part:
            a, b = part.split("-", 1)
            base = range(int(a), int(b) + 1)
        else:
            base = [int(part)]
        values.update(v for v in base if lo <= v <= hi and (v - lo) % step == 0)
    return values


class CronExpr(object):
    def __init__(self, expr):
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError("bad cron expr: %r" % expr)
        self.min = _parse_field(parts[0], 0, 59)
        self.hour = _parse_field(parts[1], 0, 23)
        self.day = _parse_field(parts[2], 1, 31)
        self.month = _parse_field(parts[3], 1, 12)
        self.dow = _parse_field(parts[4], 0, 7)
        if self.dow is not None and 7 in self.dow:
            self.dow.add(0)

    def matches(self, dt):
        if self.min is not None and dt.minute not in self.min:
            return False
        if self.hour is not None and dt.hour not in self.hour:
            return False
        if self.day is not None and dt.day not in self.day:
            return False
        if self.month is not None and dt.month not in self.month:
            return False
        # cron 星期字段: 0/7=周日, 1=周一 ... 6=周六 → 映射 Python weekday (0=周一)
        cron_dow = dt.weekday() + 1
        if cron_dow == 7:
            cron_dow = 0
        if self.dow is not None and cron_dow not in self.dow:
            return False
        return True

    def prev_before(self, dt):
        """返回 dt 之前（不含 dt 本身）最近一次匹配时间（归整到分钟）；找不到返回 None。"""
        t = dt.replace(second=0, microsecond=0) - timedelta(minutes=1)
        for _ in range(60 * 24 * 370):  # 最多往回找一年
            if self.matches(t):
                return t
            t -= timedelta(minutes=1)
        return None


def parse_ts(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        return t
    except Exception:
        return None


def load_jobs():
    with open(JOBS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("jobs", [])


def check_heartbeat(now):
    """调度器心跳检测；返回 True 表示心跳正常。"""
    try:
        with open(HEARTBEAT_FILE) as f:
            raw = f.read().strip()
        ts = float(raw.split()[0])  # 心跳文件含 ts + 空格 + 其他字段（pid 等）
        hb = datetime.fromtimestamp(ts)
        if now - hb > timedelta(minutes=HEARTBEAT_TIMEOUT_MIN):
            print("🚨 **Hermes 调度器疑似卡死**：ticker 心跳停在 %s（超过 %d 分钟）"
                  % (hb.strftime("%m-%d %H:%M"), HEARTBEAT_TIMEOUT_MIN))
            print("   cron 任务可能全部未运行，请检查 Hermes 进程状态！")
            return False
    except Exception as e:
        print("⚠️ cron_watchdog: 心跳文件读取失败: %s" % str(e)[:120])
    return True


def load_running_jobs():
    """返回正在运行（未完成）的 job_id 集合，来自 executions.db。"""
    running = set()
    db = os.path.join(CRON_DIR, "executions.db")
    if not os.path.exists(db):
        return running
    import sqlite3
    try:
        conn = sqlite3.connect(db)
        cur = conn.execute("SELECT job_id FROM executions WHERE status='running'")
        running = set(r[0] for r in cur.fetchall())
        conn.close()
    except Exception:
        pass
    return running


def main():
    now = datetime.now()
    try:
        jobs = load_jobs()
    except Exception as e:
        print("⚠️ cron_watchdog 数据源读取失败: %s" % str(e)[:200])
        return 1

    # 调度器心跳（异常时仍继续逐任务检查）
    hb_ok = check_heartbeat(now)
    running_jobs = load_running_jobs()

    alerts = []
    checked = 0
    for j in jobs:
        if not j.get("enabled", False):
            continue
        state = j.get("state", "")
        if state in ("paused", "disabled", "completed"):
            continue
        checked += 1

        jid = j.get("id", "")
        name = j.get("name", jid)
        if jid in running_jobs:
            continue  # 正在运行，不算漏跑
        sched_obj = j.get("schedule") or {}
        expr_s = sched_obj.get("expr") if isinstance(sched_obj, dict) else sched_obj
        if not expr_s:
            continue
        try:
            expr = CronExpr(expr_s)
        except Exception:
            continue

        last_status = j.get("last_status")
        last_run = parse_ts(j.get("last_run_at"))
        next_run = parse_ts(j.get("next_run_at"))

        # 1) 失败检测
        if last_status in ("error", "failed"):
            when = last_run.strftime("%m-%d %H:%M") if last_run else "未知时间"
            err = str(j.get("last_error") or "")[:ERR_TAIL].replace("\n", " ").strip()
            line = "❌ **%s** 最近执行失败（%s, %s）" % (name, when, expr_s)
            if err:
                line += "\n    ↳ %s" % err
            alerts.append(line)
            continue

        # 2) 漏跑检测
        prev = expr.prev_before(now)
        if prev is None:
            continue
        overdue = now - prev > timedelta(minutes=GRACE_MIN)
        if not overdue:
            continue
        if last_run is None:
            # 从未运行：若 next_run 已过预定时间（不是未来）→ 漏跑
            if next_run is None or next_run < now:
                alerts.append("⏰ **%s** 从未运行，预定 %s 已过 (%s)" %
                              (name, (next_run or prev).strftime("%m-%d %H:%M"), expr_s))
        elif last_run < prev:
            alerts.append("⏰ **%s** 应于 %s 运行，实际上次 %s (%s)" %
                          (name, prev.strftime("%m-%d %H:%M"),
                           last_run.strftime("%m-%d %H:%M"), expr_s))

    if alerts:
        head = "🚨 **Cron 任务巡查发现 %d 个问题** (%s)" % (len(alerts), now.strftime("%m-%d %H:%M"))
        print(head)
        print("━━━━━━━━━━━━━━━━━━━━")
        for a in alerts:
            print(a)
        print("")
        print("*巡查: %d 个 enabled 任务 | 每天 08:00 自动运行 | 正常时静默*" % checked)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
