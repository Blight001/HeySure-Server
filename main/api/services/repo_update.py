"""仓库自动更新：检测远程是否有新提交 → 拉取最新代码 → 重启全部子服务。

定位与边界
----------
- 只在「网关」进程里跑检测/拉取循环：网关持有 git 工作区，又能通过
  ``/internal/restart`` + ``process_control`` 把 4 个进程都重启。其它 runtime
  仅共享 ``api`` 层，不会触发本模块。
- 配置（开关 / 检测间隔）落在 :class:`SystemSetting` 键值表，管理员控制台可
  在运行时修改、即时生效，无需改环境变量或重启。
- 进度是**进程内单例** :data:`_state`，管理员控制台通过 ``GET
  /api/admin/repo-update/status`` 轮询读取，能实时看到「检测 → 拉取 → 重启」
  各阶段；重启会断开网关连接，前端据此显示「重启中」并在网关恢复后读到新版本。
- 拉取统一用 ``git pull --ff-only`` —— 只快进、不合并，避免覆盖本地改动；任何
  一步失败都进入 ``error`` 阶段并放弃重启。

部署注意
--------
本模块面向「同一份 git 工作区 + 多进程」（本地 run.bat / 单机部署，或带 ``.git``
的容器）场景：网关直接 ``git pull`` 后让 4 个进程各自 re-exec，无需任何外部脚本。
无 git 工作区时检测会优雅地报告不可用。
"""

from __future__ import annotations

import logging
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from sqlmodel import Session

from api.core.settings import DATA_DIR, REPOSITORY_DIR, settings
from api.database import engine
from api.services.access.auth_settings import get_setting, set_setting


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置（SystemSetting 键值）
# ---------------------------------------------------------------------------

AUTO_ENABLED_KEY = "repo_update.auto_enabled"
INTERVAL_KEY = "repo_update.interval_seconds"
LAST_AUTO_TRIGGER_AT_KEY = "repo_update.last_auto_trigger_at"
# 最近一次成功更新的痕迹——重启会清空进程内 _state，靠这几个键在重启后仍能
# 在控制台展示「上次已更新到 <sha>」。
LAST_UPDATE_AT_KEY = "repo_update.last_update_at"
LAST_UPDATE_COMMIT_KEY = "repo_update.last_update_commit"
LAST_UPDATE_FROM_KEY = "repo_update.last_update_from"

DEFAULT_INTERVAL_SECONDS = 1800
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 86400
DEPLOYED_VERSION_FILE = os.path.join(DATA_DIR, "deployed-version.json")


def get_config(session: Session) -> Dict[str, Any]:
    """读取自动更新配置（带默认值与范围约束）。"""
    auto = get_setting(session, AUTO_ENABLED_KEY, "0").strip().lower()
    auto_enabled = auto in ("1", "true", "yes", "on")
    raw_interval = get_setting(session, INTERVAL_KEY, str(DEFAULT_INTERVAL_SECONDS))
    try:
        interval = int(float(raw_interval))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_SECONDS
    interval = max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, interval))
    return {"auto_enabled": auto_enabled, "interval_seconds": interval}


def set_config(session: Session, *, auto_enabled: bool, interval_seconds: int) -> Dict[str, Any]:
    interval = max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, int(interval_seconds)))
    set_setting(session, AUTO_ENABLED_KEY, "1" if auto_enabled else "0")
    set_setting(session, INTERVAL_KEY, str(interval))
    session.commit()
    return {"auto_enabled": bool(auto_enabled), "interval_seconds": interval}


def get_last_update(session: Session) -> Dict[str, Any]:
    raw_at = get_setting(session, LAST_UPDATE_AT_KEY, "")
    try:
        at = float(raw_at) if raw_at else None
    except (TypeError, ValueError):
        at = None
    return {
        "at": at,
        "commit": get_setting(session, LAST_UPDATE_COMMIT_KEY, "") or None,
        "from": get_setting(session, LAST_UPDATE_FROM_KEY, "") or None,
    }


def _record_last_update(*, from_sha: str, to_sha: str) -> None:
    try:
        with Session(engine) as session:
            set_setting(session, LAST_UPDATE_AT_KEY, str(time.time()))
            set_setting(session, LAST_UPDATE_COMMIT_KEY, to_sha or "")
            set_setting(session, LAST_UPDATE_FROM_KEY, from_sha or "")
            session.commit()
    except Exception:
        logger.exception("failed to persist repo-update marker")


# ---------------------------------------------------------------------------
# 进度状态（进程内单例）
# ---------------------------------------------------------------------------

# 阶段：idle 空闲 / checking 检测中 / up_to_date 已是最新 / update_available 有新版本
#       / pulling 拉取中 / restarting 重启中 / error 失败
_STEP_CHECK = "check"
_STEP_PULL = "pull"
_STEP_RESTART = "restart"

_STEP_LABELS = {
    _STEP_CHECK: "检测远程更新",
    _STEP_PULL: "拉取最新代码",
    _STEP_RESTART: "重启服务",
}


def _fresh_steps() -> List[Dict[str, str]]:
    # status: pending 待执行 / active 进行中 / done 完成 / error 失败 / skipped 跳过
    return [{"key": k, "label": _STEP_LABELS[k], "status": "pending"} for k in (_STEP_CHECK, _STEP_PULL, _STEP_RESTART)]


_state: Dict[str, Any] = {
    "phase": "idle",
    "message": "",
    "running": False,
    "trigger": "",  # auto / manual
    "steps": _fresh_steps(),
    "branch": "",
    "ahead": 0,
    "behind": 0,
    "current": None,  # 当前 commit 信息
    "remote": None,  # 远程 commit 信息
    "last_check_at": None,
    "last_error": "",
    "logs": [],
    "updated_at": time.time(),
}

_state_lock = threading.Lock()
# 只允许一个更新流程在跑——非阻塞获取，避免手动按钮与自动轮询并发。
_op_lock = threading.Lock()
_last_auto_check_at = 0.0


def _set_state(**fields: Any) -> None:
    with _state_lock:
        _state.update(fields)
        _state["updated_at"] = time.time()


def _set_step(key: str, status: str) -> None:
    with _state_lock:
        for step in _state["steps"]:
            if step["key"] == key:
                step["status"] = status
        _state["updated_at"] = time.time()


def get_state() -> Dict[str, Any]:
    with _state_lock:
        # 深拷贝 steps，避免调用方拿到内部可变引用。
        snapshot = dict(_state)
        snapshot["steps"] = [dict(s) for s in _state["steps"]]
        snapshot["logs"] = list(_state.get("logs") or [])
        return snapshot


# ---------------------------------------------------------------------------
# git 封装
# ---------------------------------------------------------------------------


class RepoUpdateError(RuntimeError):
    """git 操作失败（fetch / pull / 状态异常）。"""


def _run_git(args: List[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


_git_available_cache: Optional[bool] = None


def git_available(refresh: bool = False) -> bool:
    """工作区是否是一个可用的 git 仓库（结果缓存，git 不会中途消失）。"""
    global _git_available_cache
    if _git_available_cache is not None and not refresh:
        return _git_available_cache
    available = False
    try:
        if os.path.isdir(os.path.join(REPOSITORY_DIR, ".git")):
            proc = _run_git(["rev-parse", "--is-inside-work-tree"], timeout=10)
            available = proc.returncode == 0 and proc.stdout.strip() == "true"
    except Exception:
        available = False
    _git_available_cache = available
    return available


def _current_branch() -> str:
    proc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
    branch = proc.stdout.strip()
    return branch if proc.returncode == 0 and branch and branch != "HEAD" else ""


def _commit_info(ref: str = "HEAD") -> Optional[Dict[str, Any]]:
    # %H 全 sha / %h 短 sha / %an 作者 / %ct 提交时间戳 / %s 标题
    proc = _run_git(["log", "-1", "--format=%H%n%h%n%an%n%ct%n%s", ref], timeout=15)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    parts = proc.stdout.strip().split("\n", 4)
    if len(parts) < 5:
        return None
    sha, short, author, ts, subject = parts
    try:
        committed_at = float(ts)
    except (TypeError, ValueError):
        committed_at = None
    body_proc = _run_git(["show", "-s", "--format=%B", ref], timeout=15)
    body = body_proc.stdout.strip() if body_proc.returncode == 0 else subject
    files: List[Dict[str, Any]] = []
    stat_proc = _run_git(["show", "--format=", "--numstat", ref], timeout=30)
    if stat_proc.returncode == 0:
        for line in stat_proc.stdout.splitlines()[:200]:
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            files.append({
                "path": path,
                "added": None if added == "-" else int(added),
                "deleted": None if deleted == "-" else int(deleted),
            })
    return {
        "sha": sha,
        "short": short,
        "author": author,
        "committed_at": committed_at,
        "subject": subject,
        "body": body,
        "files": files,
    }


def collect_version_info() -> Dict[str, Any]:
    """当前工作区版本快照（无需联网），用于进入栏目时展示。"""
    remote = _remote_version_info()
    if remote is not None:
        return remote
    if not git_available():
        try:
            with open(DEPLOYED_VERSION_FILE, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            current = payload.get("current")
            if isinstance(payload, dict) and isinstance(current, dict):
                return {
                    "git_available": False,
                    "branch": str(payload.get("branch") or ""),
                    "current": current,
                }
        except (OSError, ValueError, TypeError):
            pass
        return {"git_available": False, "branch": "", "current": None}
    return {
        "git_available": True,
        "branch": _current_branch(),
        "current": _commit_info("HEAD"),
    }


def update_mode() -> str:
    """Return the configured deployment updater: remote, git or unavailable."""
    if remote_updater_available():
        return "remote"
    return "git" if git_available() else "unavailable"


def updater_available() -> bool:
    return update_mode() != "unavailable"


def remote_updater_available() -> bool:
    if not settings.repo_updater_url:
        return False
    try:
        payload = _remote_request("GET", "/health", auth=False, timeout=3)
        return bool(payload.get("ok"))
    except Exception:
        return False


def _remote_auth_token() -> str:
    return settings.repo_updater_token or settings.internal_token


def _remote_request(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    *,
    auth: bool = True,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    base = settings.repo_updater_url.rstrip("/")
    if not base:
        raise RepoUpdateError("未配置宿主更新服务 HEYSURE_REPO_UPDATER_URL")
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = _remote_auth_token()
    if auth and token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RepoUpdateError(f"宿主更新服务返回 {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RepoUpdateError(f"无法连接宿主更新服务：{exc.reason}") from exc
    try:
        payload = json.loads(raw or "{}")
    except ValueError as exc:
        raise RepoUpdateError("宿主更新服务返回了非 JSON 响应") from exc
    if not isinstance(payload, dict):
        raise RepoUpdateError("宿主更新服务响应格式无效")
    if payload.get("ok") is False and payload.get("error"):
        raise RepoUpdateError(str(payload.get("error")))
    return payload


def _remote_version_info() -> Optional[Dict[str, Any]]:
    if not settings.repo_updater_url:
        return None
    try:
        payload = _remote_request("GET", "/version", timeout=8)
        return {
            "git_available": False,
            "updater_available": True,
            "branch": str(payload.get("branch") or ""),
            "current": payload.get("current") if isinstance(payload.get("current"), dict) else None,
        }
    except Exception as exc:
        logger.warning("repo-update: remote version unavailable: %s", exc)
        return None


def _fetch_and_compare() -> Dict[str, Any]:
    """``git fetch`` 后比较本地与远程，返回领先/落后提交数及双方 commit 信息。"""
    branch = _current_branch()
    fetch_args = ["fetch", "--quiet", "origin", branch] if branch else ["fetch", "--quiet", "origin"]
    proc = _run_git(fetch_args, timeout=180)
    if proc.returncode != 0:
        raise RepoUpdateError(f"git fetch 失败：{(proc.stderr or proc.stdout).strip()}")

    upstream = f"origin/{branch}" if branch else ""
    if not upstream:
        # detached HEAD / 无分支名：退回 @{u} 上游引用
        up = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=15)
        upstream = up.stdout.strip() if up.returncode == 0 else ""
    if not upstream:
        raise RepoUpdateError("无法确定上游分支（origin/<branch>）")

    counts = _run_git(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], timeout=30)
    ahead = behind = 0
    if counts.returncode == 0 and counts.stdout.strip():
        left, _, right = counts.stdout.strip().partition("\t")
        if not right:
            left, _, right = counts.stdout.strip().partition(" ")
        try:
            ahead, behind = int(left), int(right)
        except (TypeError, ValueError):
            ahead = behind = 0
    return {
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "current": _commit_info("HEAD"),
        "remote": _commit_info(upstream),
    }


def _pull_fast_forward(branch: str) -> None:
    args = ["pull", "--ff-only", "origin", branch] if branch else ["pull", "--ff-only"]
    proc = _run_git(args, timeout=300)
    if proc.returncode != 0:
        raise RepoUpdateError(f"git pull 失败：{(proc.stderr or proc.stdout).strip()}")


def _run_remote_check_and_maybe_update(*, trigger: str, auto_apply: bool) -> Dict[str, Any]:
    _set_state(
        phase="checking",
        running=True,
        trigger=trigger,
        steps=_fresh_steps(),
        last_error="",
        message="正在通过宿主更新服务检测远程更新…",
    )
    _set_step(_STEP_CHECK, "active")
    payload = _remote_request("POST", "/check", {"apply": auto_apply}, timeout=600)
    _set_state(
        branch=str(payload.get("branch") or ""),
        ahead=int(payload.get("ahead") or 0),
        behind=int(payload.get("behind") or 0),
        current=payload.get("current") if isinstance(payload.get("current"), dict) else None,
        remote=payload.get("remote") if isinstance(payload.get("remote"), dict) else None,
        last_check_at=time.time(),
    )
    _set_step(_STEP_CHECK, "done")

    if not payload.get("updated") and not payload.get("update_available"):
        _set_state(phase="up_to_date", running=False, message="已是最新版本")
        _set_step(_STEP_PULL, "skipped")
        _set_step(_STEP_RESTART, "skipped")
        return {"ok": True, "updated": False, "update_available": False, "state": get_state()}

    if payload.get("update_available") and not auto_apply:
        _set_state(
            phase="update_available",
            running=False,
            message=f"发现 {int(payload.get('behind') or 0)} 个新提交，待更新",
        )
        return {"ok": True, "updated": False, "update_available": True, "state": get_state()}

    from_sha = str(payload.get("from") or ((payload.get("remote") or {}).get("sha") if isinstance(payload.get("remote"), dict) else "") or "")
    to_sha = str(payload.get("to") or ((payload.get("current") or {}).get("sha") if isinstance(payload.get("current"), dict) else "") or "")
    if to_sha:
        _record_last_update(from_sha=from_sha, to_sha=to_sha)
    _set_step(_STEP_PULL, "done")
    _set_state(phase="restarting", message="代码已更新，宿主更新服务正在重建并重启容器…")
    _set_step(_STEP_RESTART, "active")
    return {"ok": True, "updated": True, "restarting": True, "state": get_state()}


# ---------------------------------------------------------------------------
# 重启全部子服务
# ---------------------------------------------------------------------------


def _restart_all_services() -> None:
    """先重启远端 runtime（mcp/connector/ai），最后让网关自身 re-exec。

    网关重启会替换当前进程映像，所以放最后；远端 runtime 通过各自的
    ``/internal/restart`` 自我 re-exec。docker 拆分部署下远端是独立镜像，
    重启只是用各自已有代码重来一遍（见模块顶部「部署注意」）。
    """
    from api.runtime.internal_http import InternalClient

    remotes = [
        ("mcp", settings.mcp_runtime_url),
        ("connector", settings.connector_runtime_url),
        ("ai", settings.ai_runtime_url),
    ]
    for key, url in remotes:
        if not url:
            continue
        client = InternalClient(url, timeout=5.0)
        try:
            client.post("/internal/restart")
            logger.warning("repo-update: requested restart of %s (%s)", key, url)
        except Exception as exc:
            # 单个 runtime 重启失败不阻断整体：docker restart 策略会兜底。
            logger.warning("repo-update: restart of %s failed: %s", key, exc)
        finally:
            client.close()

    from api.runtime.process_control import request_restart

    # 给 HTTP 响应留出 flush 时间，网关稍后再 re-exec。
    request_restart(delay=2.0)


# ---------------------------------------------------------------------------
# 编排：检测（→ 拉取 → 重启）
# ---------------------------------------------------------------------------


def run_check_and_maybe_update(*, trigger: str, auto_apply: bool) -> Dict[str, Any]:
    """执行一次「检测」，``auto_apply`` 为真且发现新版本时继续「拉取 + 重启」。

    返回本次结果摘要；详细进度通过 :func:`get_state` 暴露给前端轮询。
    并发保护：已有流程在跑时直接返回当前状态（不重复触发）。
    """
    mode = update_mode()
    if mode == "unavailable":
        error = "未连接宿主更新服务，且当前部署不是可用的 git 工作区"
        _set_state(phase="error", running=False, last_error=error, message="更新器不可用")
        return {"ok": False, "error": error, "state": get_state()}

    if not _op_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "state": get_state()}

    try:
        if mode == "remote":
            return _run_remote_check_and_maybe_update(trigger=trigger, auto_apply=auto_apply)

        _set_state(
            phase="checking",
            running=True,
            trigger=trigger,
            steps=_fresh_steps(),
            last_error="",
            message="正在检测远程更新…",
        )
        _set_step(_STEP_CHECK, "active")

        info = _fetch_and_compare()
        _set_state(
            branch=info["branch"],
            ahead=info["ahead"],
            behind=info["behind"],
            current=info["current"],
            remote=info["remote"],
            last_check_at=time.time(),
        )
        _set_step(_STEP_CHECK, "done")

        if info["behind"] <= 0:
            _set_state(phase="up_to_date", running=False, message="已是最新版本")
            _set_step(_STEP_PULL, "skipped")
            _set_step(_STEP_RESTART, "skipped")
            return {"ok": True, "updated": False, "update_available": False, "state": get_state()}

        if not auto_apply:
            _set_state(
                phase="update_available",
                running=False,
                message=f"发现 {info['behind']} 个新提交，待更新",
            )
            return {"ok": True, "updated": False, "update_available": True, "state": get_state()}

        # ---- 有新版本且允许自动应用：拉取 ----
        from_sha = (info["current"] or {}).get("sha", "")
        _set_state(phase="pulling", message=f"正在拉取 {info['behind']} 个新提交…")
        _set_step(_STEP_PULL, "active")
        _pull_fast_forward(info["branch"])
        _set_step(_STEP_PULL, "done")

        new_info = _commit_info("HEAD")
        to_sha = (new_info or {}).get("sha", "")
        _record_last_update(from_sha=from_sha, to_sha=to_sha)
        _set_state(current=new_info)

        # ---- 重启 ----
        _set_state(phase="restarting", message="代码已更新，正在重启服务…")
        _set_step(_STEP_RESTART, "active")
        logger.warning("repo-update(%s): pulled %s -> %s, restarting services", trigger, from_sha[:8], to_sha[:8])
        _restart_all_services()
        # 网关将在数秒内 re-exec；此处不再清 running，让前端在重启窗口内
        # 持续看到「重启中」，待网关恢复后状态自然重置为 idle。
        return {"ok": True, "updated": True, "restarting": True, "state": get_state()}

    except RepoUpdateError as exc:
        logger.warning("repo-update(%s) failed: %s", trigger, exc)
        _set_state(phase="error", running=False, last_error=str(exc), message="更新失败")
        # 把当前进行中的步骤标红
        with _state_lock:
            for step in _state["steps"]:
                if step["status"] == "active":
                    step["status"] = "error"
        return {"ok": False, "error": str(exc), "state": get_state()}
    except Exception as exc:  # 兜底，避免线程里抛出未捕获异常
        logger.exception("repo-update(%s) unexpected error", trigger)
        _set_state(phase="error", running=False, last_error=str(exc), message="更新异常")
        return {"ok": False, "error": str(exc), "state": get_state()}
    finally:
        _op_lock.release()


def trigger_async(*, trigger: str, auto_apply: bool) -> None:
    """在后台线程里跑 :func:`run_check_and_maybe_update`（git/重启是阻塞操作）。"""
    threading.Thread(
        target=run_check_and_maybe_update,
        kwargs={"trigger": trigger, "auto_apply": auto_apply},
        name="repo-update",
        daemon=True,
    ).start()


def maybe_auto_check() -> None:
    """供网关周期循环调用：到点且开关打开就触发一次自动「检测 + 更新」。

    本身很轻（只读一次配置 + 时间比较），真正的 git/重启在后台线程里执行，
    不会阻塞事件循环。
    """
    global _last_auto_check_at
    if not updater_available():
        return
    if _state.get("running"):
        return
    try:
        with Session(engine) as session:
            cfg = get_config(session)
            raw_last_trigger = get_setting(session, LAST_AUTO_TRIGGER_AT_KEY, "")
            try:
                persisted_last_trigger = float(raw_last_trigger) if raw_last_trigger else 0.0
            except (TypeError, ValueError):
                persisted_last_trigger = 0.0
    except Exception:
        logger.exception("repo-update: failed to read config")
        return
    if not cfg["auto_enabled"]:
        return
    now = time.time()
    _last_auto_check_at = max(_last_auto_check_at, persisted_last_trigger)
    if now - _last_auto_check_at < cfg["interval_seconds"]:
        return
    _last_auto_check_at = now
    try:
        with Session(engine) as session:
            set_setting(session, LAST_AUTO_TRIGGER_AT_KEY, str(now))
            session.commit()
    except Exception:
        logger.exception("repo-update: failed to persist auto trigger time")
        return
    logger.info("repo-update: auto check fired (interval=%ss)", cfg["interval_seconds"])
    trigger_async(trigger="auto", auto_apply=True)
