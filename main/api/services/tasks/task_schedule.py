"""任务定时/循环的统一 schedule 模块（唯一权威定义）。

之前 schedule 的解析/校验/续期散落在 REST 路由、MCP 工具与 chat_runtime
三处且互不一致；本模块收拢为单一实现，所有入口共用：

- REST:  gateway/routers/ai_task_routes.py（task-trigger / PATCH task-jobs）
- MCP:   mcp_runtime/mcp/tools/tasks.py（task.create 等）
- 续期:  api/chat_runtime/chat_runtime_helpers._renew_loop_scheduled_job
- 调度:  api/chat_runtime/chat_scheduler._is_job_time_ready

task_payload["schedule"] 的规范结构::

    {
      "enabled": bool,            # 是否启用定时/循环
      "loop_enabled": bool,       # 是否循环（完成后自动创建下一轮）
      "loop_mode": str,           # 循环方式: interval / daily / weekly
      "run_immediately": bool,    # 循环任务首轮是否立即执行
      "duration_minutes": int,    # interval 循环间隔 / 单次定时延迟（分钟）
      "daily_time": "HH:MM",      # daily/weekly 循环每次触发时刻（schedule_timezone 时区，默认 Asia/Shanghai）
      "weekly_days": [int],       # weekly 循环触发的星期（0=周一 ... 6=周日）
      "max_runs": int,            # 循环总轮数上限（0=不限）
      "runs_done": int,           # 已完成轮数（续期时 +1，由系统维护）
      "end_at": float|None,       # 循环截止时间戳；下一轮超过该时刻则停止续期
      "schedule_at": float|None,  # 下一次执行时刻（创建/续期时统一计算）
    }

循环方式：
- interval: 每轮完成后间隔 duration_minutes 分钟再次执行（历史唯一方式）
- daily:    每天 daily_time 执行一轮
- weekly:   每周 weekly_days 指定的星期在 daily_time 执行一轮
"""

import time
from datetime import datetime, timedelta, tzinfo
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DURATION_MINUTES = 30
MAX_DURATION_MINUTES = 7 * 24 * 60
LOOP_MODES = ("interval", "daily", "weekly")

# 入参字段别名（统一收口；新代码请使用每组第一个标准名）
ENABLED_KEYS: Tuple[str, ...] = ("schedule_enabled", "enabled", "enable", "is_enabled", "active", "on")
AT_KEYS: Tuple[str, ...] = ("schedule_at", "run_at", "schedule_time")
DURATION_KEYS: Tuple[str, ...] = ("schedule_duration_minutes", "duration_minutes", "interval_minutes")
LOOP_ENABLED_KEYS: Tuple[str, ...] = ("schedule_loop_enabled", "loop_enabled", "loop", "repeat", "recurring")
RUN_IMMEDIATELY_KEYS: Tuple[str, ...] = (
    "schedule_run_immediately", "run_immediately", "first_run_immediately", "immediate", "run_now",
)
LOOP_MODE_KEYS: Tuple[str, ...] = ("schedule_loop_mode", "loop_mode")
DAILY_TIME_KEYS: Tuple[str, ...] = ("schedule_daily_time", "daily_time", "loop_time")
WEEKLY_DAYS_KEYS: Tuple[str, ...] = ("schedule_weekly_days", "weekly_days", "loop_weekdays")
MAX_RUNS_KEYS: Tuple[str, ...] = ("schedule_max_runs", "max_runs", "loop_max_runs")
END_AT_KEYS: Tuple[str, ...] = ("schedule_end_at", "end_at", "loop_end_at")

_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def schedule_timezone() -> Optional[tzinfo]:
    """定时/循环墙钟时间（daily_time 等）所用时区。

    读 settings.timezone（默认 Asia/Shanghai）。容器常跑 UTC，若不固定
    时区，用户配置的"每天 10:00"会被按 UTC 计算，展示到浏览器就偏成
    18:00。未配置或时区名非法时返回 None（退回服务器本地时区，即旧行为）。
    """
    try:
        from api.core.settings import settings

        name = str(getattr(settings, "timezone", "") or "").strip()
    except Exception:
        name = ""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


def format_schedule_ts(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """按 schedule_timezone 把时间戳格式化成人类可读时间（展示统一入口）。"""
    ts = parse_timestamp(value)
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, schedule_timezone()).strftime(fmt)


def _parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _parse_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def parse_timestamp(value: Any) -> Optional[float]:
    """宽松时间戳解析：Unix 秒或 ISO-8601；无时区按 schedule_timezone。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
        return parsed if parsed > 0 else None
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            tz = schedule_timezone()
            if tz is not None:
                dt = dt.replace(tzinfo=tz)
        return dt.timestamp()
    except Exception:
        return None


def parse_timestamp_strict(value: Any) -> Tuple[Optional[float], Optional[str], bool]:
    """严格时间戳解析（MCP 入口）：返回 (时间戳, 错误信息, 是否提供了输入)。

    AI 传入的字符串时间必须带时区，避免模型按错误时区推算。
    """
    if value is None:
        return None, None, False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, None, False
    else:
        text = str(value).strip()
    provided = True

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts <= 0:
            return None, "时间必须是正数时间戳（Unix 秒）", provided
        return ts, None, provided

    try:
        ts = float(text)
        if ts <= 0:
            return None, "时间必须是正数时间戳（Unix 秒）", provided
        return ts, None, provided
    except Exception:
        pass

    iso_text = text
    if iso_text.endswith("Z"):
        iso_text = iso_text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso_text)
    except Exception:
        return None, "时间字符串必须是 ISO-8601（示例: 2026-03-24T16:30:00+08:00）", provided

    if dt.tzinfo is None or dt.utcoffset() is None:
        return None, "时间使用 ISO-8601 时必须包含时区（+08:00 或 Z）", provided
    return dt.timestamp(), None, provided


def parse_daily_time(value: Any) -> Optional[Tuple[int, int]]:
    """解析 "HH:MM" → (hour, minute)；非法返回 None。"""
    text = str(value or "").strip()
    if not text or ":" not in text:
        return None
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except Exception:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def parse_weekly_days(value: Any) -> List[int]:
    """解析星期列表 → 去重升序的 [0-6]（0=周一 ... 6=周日）。"""
    items: List[Any]
    if isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, str):
        items = [part for part in value.replace("，", ",").split(",") if part.strip()]
    elif isinstance(value, (int, float)):
        items = [value]
    else:
        return []
    out: set[int] = set()
    for item in items:
        try:
            day = int(item)
        except Exception:
            continue
        if 0 <= day <= 6:
            out.add(day)
    return sorted(out)


def _pick(source: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    if not isinstance(source, dict):
        return None
    for key in keys:
        if key in source:
            return source.get(key)
    return None


def _has_key(source: Dict[str, Any], keys: Tuple[str, ...]) -> bool:
    return isinstance(source, dict) and any(key in source for key in keys)


def extract_schedule(source: Dict[str, Any]) -> Dict[str, Any]:
    """从请求体/工具入参提取规范 schedule（兼容平铺 schedule_* 与嵌套 schedule 对象）。

    未显式提供 enabled 时，只要带任何定时/循环参数即视为启用，
    避免 AI 传了参数却悄悄落成立即任务。
    """
    body = source if isinstance(source, dict) else {}
    nested_raw = body.get("schedule")
    nested = nested_raw if isinstance(nested_raw, dict) else {}

    def pick(keys: Tuple[str, ...]) -> Any:
        value = _pick(nested, keys)
        if value is not None:
            return value
        return _pick(body, keys)

    schedule_at = parse_timestamp(pick(AT_KEYS))
    duration_minutes = _parse_int(pick(DURATION_KEYS), DEFAULT_DURATION_MINUTES, 1, MAX_DURATION_MINUTES)
    loop_enabled = _parse_bool(pick(LOOP_ENABLED_KEYS), False)
    run_immediately = _parse_bool(pick(RUN_IMMEDIATELY_KEYS), False)
    loop_mode = str(pick(LOOP_MODE_KEYS) or "").strip().lower()
    if loop_mode not in LOOP_MODES:
        loop_mode = "interval"
    daily_raw = pick(DAILY_TIME_KEYS)
    daily = parse_daily_time(daily_raw)
    weekly_days = parse_weekly_days(pick(WEEKLY_DAYS_KEYS))
    max_runs = _parse_int(pick(MAX_RUNS_KEYS), 0, 0, 100000)
    end_at = parse_timestamp(pick(END_AT_KEYS))

    enabled_raw = pick(ENABLED_KEYS)
    has_hint = (
        schedule_at is not None
        or loop_enabled
        or run_immediately
        or _has_key(nested, DURATION_KEYS) or _has_key(body, DURATION_KEYS)
        or daily is not None
        or bool(weekly_days)
    )
    if enabled_raw is None:
        enabled = has_hint
    else:
        enabled = _parse_bool(enabled_raw, has_hint)

    runs_done = _parse_int(_pick(nested, ("runs_done",)), 0, 0, 10**9)

    return normalize_schedule({
        "enabled": enabled,
        "loop_enabled": loop_enabled,
        "loop_mode": loop_mode,
        "run_immediately": run_immediately,
        "duration_minutes": duration_minutes,
        "daily_time": f"{daily[0]:02d}:{daily[1]:02d}" if daily else "",
        "weekly_days": weekly_days,
        "max_runs": max_runs,
        "runs_done": runs_done,
        "end_at": end_at,
        "schedule_at": schedule_at,
    })


def normalize_schedule(raw: Any) -> Dict[str, Any]:
    """把任意（历史/外部）schedule dict 规范成完整结构，保证字段齐全合法。"""
    src = raw if isinstance(raw, dict) else {}
    enabled = _parse_bool(src.get("enabled"), False)
    loop_enabled = enabled and _parse_bool(src.get("loop_enabled"), False)
    loop_mode = str(src.get("loop_mode") or "").strip().lower()
    if loop_mode not in LOOP_MODES:
        loop_mode = "interval"
    daily = parse_daily_time(src.get("daily_time"))
    weekly_days = parse_weekly_days(src.get("weekly_days"))
    # daily/weekly 缺少必要字段时回落 interval，调度永不因脏数据卡死
    if loop_mode == "daily" and daily is None:
        loop_mode = "interval"
    if loop_mode == "weekly" and (daily is None or not weekly_days):
        loop_mode = "interval"
    return {
        "enabled": enabled,
        "loop_enabled": loop_enabled,
        # loop_mode 不随 loop_enabled 清空：mode 切换（如 task.update 改为
        # recurring）时需要保留已填写的循环方式
        "loop_mode": loop_mode,
        "run_immediately": loop_enabled and _parse_bool(src.get("run_immediately"), False),
        "duration_minutes": _parse_int(src.get("duration_minutes"), DEFAULT_DURATION_MINUTES, 1, MAX_DURATION_MINUTES),
        "daily_time": f"{daily[0]:02d}:{daily[1]:02d}" if daily else "",
        "weekly_days": weekly_days,
        "max_runs": _parse_int(src.get("max_runs"), 0, 0, 100000),
        "runs_done": _parse_int(src.get("runs_done"), 0, 0, 10**9),
        "end_at": parse_timestamp(src.get("end_at")),
        "schedule_at": parse_timestamp(src.get("schedule_at")),
    }


def next_loop_occurrence(schedule: Dict[str, Any], now: float) -> float:
    """按循环方式计算 now 之后最近一次触发时刻（时区见 schedule_timezone）。"""
    loop_mode = str(schedule.get("loop_mode") or "interval")
    if loop_mode in {"daily", "weekly"}:
        daily = parse_daily_time(schedule.get("daily_time"))
        if daily is not None:
            hour, minute = daily
            base = datetime.fromtimestamp(now, schedule_timezone())
            candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if loop_mode == "daily":
                if candidate.timestamp() <= now:
                    candidate += timedelta(days=1)
                return candidate.timestamp()
            weekly_days = parse_weekly_days(schedule.get("weekly_days"))
            if weekly_days:
                for offset in range(8):
                    cand = candidate + timedelta(days=offset)
                    if cand.weekday() in weekly_days and cand.timestamp() > now:
                        return cand.timestamp()
    duration_minutes = _parse_int(schedule.get("duration_minutes"), DEFAULT_DURATION_MINUTES, 1, MAX_DURATION_MINUTES)
    return float(now + duration_minutes * 60)


def finalize_schedule(schedule: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """补全 schedule_at（下一次执行时刻），创建/编辑任务时统一调用。

    - 未启用定时：schedule_at=None
    - 循环且首轮立即执行：schedule_at=now
    - daily/weekly 循环：按时刻表计算（忽略显式 schedule_at）
    - 其余：显式 schedule_at 优先，否则 now + duration_minutes
    """
    now = float(now if now is not None else time.time())
    out = normalize_schedule(schedule)
    if not out["enabled"]:
        out["schedule_at"] = None
        return out
    if out["run_immediately"]:
        out["schedule_at"] = now
        return out
    if out["loop_enabled"] and out["loop_mode"] in {"daily", "weekly"}:
        out["schedule_at"] = next_loop_occurrence(out, now)
        return out
    explicit = out.get("schedule_at")
    if explicit is not None and explicit > 0:
        return out
    out["schedule_at"] = next_loop_occurrence(out, now)
    return out


def build_next_loop_schedule(schedule: Any, now: float) -> Optional[Dict[str, Any]]:
    """循环任务完成后计算下一轮 schedule；循环已结束时返回 None。

    结束条件：max_runs 已跑满，或下一轮触发时刻晚于 end_at。
    """
    current = normalize_schedule(schedule)
    if not current["enabled"] or not current["loop_enabled"]:
        return None
    completed_runs = current["runs_done"] + 1
    max_runs = current["max_runs"]
    if max_runs > 0 and completed_runs >= max_runs:
        return None
    next_at = next_loop_occurrence(current, now)
    end_at = current.get("end_at")
    if end_at is not None and next_at > float(end_at):
        return None
    out = dict(current)
    # "首轮立即执行"仅作用于首次创建；续期统一按时刻表触发
    out["run_immediately"] = False
    out["runs_done"] = completed_runs
    out["schedule_at"] = float(next_at)
    return out


def is_time_ready(schedule: Any, *, created_at: Optional[float], now: float) -> bool:
    """调度器判定：任务的定时时刻是否已到。"""
    sched = normalize_schedule(schedule)
    if not sched["enabled"]:
        return True
    schedule_at = sched.get("schedule_at")
    if not schedule_at or schedule_at <= 0:
        # 历史数据缺 schedule_at：按创建时间 + 间隔回算
        schedule_at = float(created_at or now) + sched["duration_minutes"] * 60
    return now >= float(schedule_at)


def describe_schedule(schedule: Any) -> str:
    """生成人类可读的定时/循环摘要（前端标签、完成通知共用）。"""
    sched = normalize_schedule(schedule)
    if not sched["enabled"]:
        return ""
    parts: List[str] = []
    if sched["loop_enabled"]:
        mode = sched["loop_mode"]
        if mode == "daily":
            parts.append(f"每天 {sched['daily_time']} 循环")
        elif mode == "weekly":
            days = "、".join(_WEEKDAY_LABELS[d] for d in sched["weekly_days"])
            parts.append(f"每周{days} {sched['daily_time']} 循环")
        else:
            parts.append(f"每 {sched['duration_minutes']} 分钟循环")
        if sched["max_runs"] > 0:
            parts.append(f"共 {sched['max_runs']} 轮（已完成 {sched['runs_done']} 轮）")
        if sched["end_at"]:
            parts.append("截止 " + format_schedule_ts(sched["end_at"]))
    elif sched["schedule_at"]:
        parts.append("定时 " + format_schedule_ts(sched["schedule_at"]))
    return "；".join(parts)
