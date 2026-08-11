"""Host-side full Compose rebuild command used by the admin gateway."""

from typing import Any, Dict

from api.core.settings import settings
from api.services.repo_update import RepoUpdateError, _remote_request


def rebuild_all_containers() -> Dict[str, Any]:
    """Queue a host-side rebuild and recreation of every Compose service."""
    if not settings.repo_updater_url:
        raise RepoUpdateError("未配置宿主更新服务 HEYSURE_REPO_UPDATER_URL")
    return _remote_request("POST", "/rebuild", {}, timeout=10)
