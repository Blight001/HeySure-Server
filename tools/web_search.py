from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException
from sqlmodel import Session, select

from api.core.settings import settings
from api.database import engine
from api.models import User


def _get_tavily_api_key(user_id: int) -> str:
    with Session(engine) as session:
        user = session.exec(select(User).where(User.id == user_id)).first()
    key = str(getattr(user, "tavily_api_key", "") or "").strip()
    return key or settings.tavily_api_key


async def _web_search(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int] = None) -> Dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")

    api_key = _get_tavily_api_key(user_id)
    if not api_key:
        raise HTTPException(status_code=400, detail="Tavily API key is not configured in system settings")

    search_depth = str(args.get("search_depth") or "advanced").strip().lower()
    if search_depth not in {"basic", "advanced"}:
        search_depth = "advanced"

    try:
        max_results = int(args.get("max_results") or 5)
    except Exception:
        max_results = 5
    max_results = max(1, min(20, max_results))

    payload: Dict[str, Any] = {
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
    }
    for name in ("include_answer", "include_raw_content", "include_images"):
        if name in args:
            payload[name] = bool(args.get(name))

    try:
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            response = await client.post(
                settings.tavily_api_url,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("search API returned a non-object response")
            return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Search API request failed: {exc}") from exc
