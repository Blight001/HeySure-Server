"""HTTP byte transport for the canonical member-transfer MCP protocol."""

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from api.services.storage.member_transfer import download_transfer, receive_upload


router = APIRouter()
PREFIX = "/api"


@router.get("/transfers/{transfer_id}/{token}")
def get_transfer(transfer_id: str, token: str) -> FileResponse:
    """Stream a prepared server-to-device transfer without loading it in memory."""
    return download_transfer(transfer_id, token)


@router.put("/transfers/{transfer_id}/{token}")
async def put_transfer(transfer_id: str, token: str, request: Request):
    """Receive one raw byte stream for a prepared device-to-server transfer."""
    return await receive_upload(transfer_id, token, request)
