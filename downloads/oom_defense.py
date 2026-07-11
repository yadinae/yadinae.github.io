#!/usr/bin/env python3
"""
oom_defense.py — OOM主动防御 (v187#3, 增强v191#4, 2026-06-28静默化)

基于系统内存使用率，主动触发GC防止OOM。
告警已统一到 health_anomaly_push.py --daily-summary。

策略:
  - memory > 90% → 主动 gc.collect()
  - 每次检查: 保护关键服务oom_score_adj + 检测重复进程
  - 每15分钟由cron轮询

用法:
  python3 scripts/oom_defense.py check          # 单次检查
"""

import os, sys, json, gc, time, argparse
from datetime import datetime

# ── 确保项目根目录在 sys.path 中 ──
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

LOG_PATH = os.path.join(_PROJECT_ROOT, "temp", "oom_defense.log")


def log(msg: str):
    """追加日志"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def get_memory() -> dict:
    """获取内存信息"""
    import psutil
    mem = psutil.virtual_memory()
    return {
        "percent": mem.percent,
        "available_mb": mem.available / 1024 / 1024,
        "total_mb": mem.total / 1024 / 1024,
        "used_mb": mem.used / 1024 / 1024,
    }


_CRITICAL_PROCS = {
    "agentmain.py": -200,
    "scheduler.py": -150,
    "health_dashboard": -100,
    "health_server": -100,
    "ga_control_center": -100,
    "gateway run": -50,
    "fsapp.py": -50,
}


def protect_critical_processes():
    """为核心服务设置 oom_score_adj，防止被 OOM killer 误杀"""
    import psutil
    protected = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            for key, adj in _CRITICAL_PROCS.items():
                if key in cmdline:
                    current = open(f"/proc/{proc.info['pid']}/oom_score_adj").read().strip()
                    if current != str(adj):
                        with open(f"/proc/{proc.info['pid']}/oom_score_adj", "w") as f:
                            f.write(str(adj))
                        protected.append((proc.info["pid"], key, adj))
        except (psutil.NoSuchProcess, PermissionError, OSError):
            continue
    if protected:
        for pid, name, adj in protected:
            log(f"🛡️  保护 PID {pid} ({name}) → oom_score_adj={adj}")
    return protected


def detect_zombie_duplicates() -> list:
    """检测重复/僵尸进程（相同cmdline的多实例）"""
    import psutil
    from collections import Counter
    proc_map = {}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if cmdline in proc_map:
                proc_map[cmdline].append(proc.info["pid"])
            else:
                proc_map[cmdline] = [proc.info["pid"]]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    warnings = []
    for cmdline, pids in proc_map.items():
        if len(pids) > 1 and any(k in cmdline for k in _CRITICAL_PROCS):
            warnings.append({
                "cmdline": cmdline[:120],
                "pids": pids,
                "count": len(pids),
                "suggestion": f"发现 {len(pids)} 个重复进程，建议 kill 旧实例"
            })
            log(f"⚠️  重复进程: {cmdline[:80]} (PIDs: {pids})")
    return warnings


def trigger_gc() -> dict:
    """主动执行 Python GC，返回统计"""
    before = gc.get_count()
    t0 = time.time()
    collected = gc.collect()
    elapsed = time.time() - t0
    after = gc.get_count()
    result = {
        "collected_objects": collected,
        "gc_generations_before": list(before),
        "gc_generations_after": list(after),
        "elapsed_sec": round(elapsed, 3),
    }
    log(f"🧹 GC 回收 {collected} 个对象 ({elapsed:.3f}s)")
    return result


def check() -> dict:
    """主检查逻辑。返回状态"""
    # 先执行进程级防御
    protected = protect_critical_processes()
    zombie_warnings = detect_zombie_duplicates()

    mem = get_memory()
    percent = mem["percent"]
    available = mem["available_mb"]
    total = mem["total_mb"]

    result = {
        "memory_percent": percent,
        "available_mb": round(available, 1),
        "total_mb": round(total, 0),
        "action": "none",
        "protected": len(protected),
        "zombie_warnings": len(zombie_warnings),
    }

    mem_summary = f"内存: {percent:.1f}% ({available:.0f}MB/{total:.0f}MB 可用)"

    if percent > 90:
        # ── 危险: >90% — 触发GC ──
        log(f"🔴 CRITICAL: {mem_summary}")
        gc_result = trigger_gc()
        log(f"✅ GC 回收 {gc_result['collected_objects']} 个对象 ({gc_result['elapsed_sec']:.3f}s)")
        result["action"] = "gc_triggered"
        result["gc_result"] = gc_result

    elif percent > 85:
        # ── 预警: >85% — 仅记录日志，不推送 ──
        log(f"🟡 WARNING: {mem_summary}")

    else:
        log(f"🟢 OK: {mem_summary} | {len(protected)}进程受保护 | {len(zombie_warnings)}重复检测")

    return result


def main():
    parser = argparse.ArgumentParser(description="OOM主动防御")
    parser.add_argument("action", choices=["check"], help="执行操作")
    args = parser.parse_args()

    if args.action == "check":
        result = check()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
