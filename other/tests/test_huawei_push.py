import asyncio

from api.services.notifications import huawei_push


def test_build_message_contains_only_device_safe_fields():
    payload = huawei_push.build_message(
        [" token-a ", "", "token-b"],
        {
            "notification_id": "notice-1",
            "kind": "message",
            "title": "贝塔发来消息",
            "body": "任务完成",
            "action_url": "/notifications/notice-1",
            "attachments": [{"server_path": "/secret/report.pdf"}],
        },
    )
    message = payload["message"]
    assert message["token"] == ["token-a", "token-b"]
    assert message["notification"]["title"] == "贝塔发来消息"
    assert "secret" not in message["data"]
    assert message["android"]["notification"]["click_action"] == {"type": 3}


def test_send_notification_uses_oauth_without_exposing_secret(monkeypatch):
    calls = []

    async def fake_request(url, *, data, headers, form=False):
        calls.append((url, data, headers, form))
        if form:
            return 200, {"access_token": "access-token", "expires_in": 3600}
        return 200, {"code": huawei_push.SUCCESS_CODE}

    monkeypatch.setattr(huawei_push.settings, "huawei_push_client_id", "client-id")
    monkeypatch.setattr(huawei_push.settings, "huawei_push_client_secret", "client-secret")
    monkeypatch.setattr(huawei_push, "_request_json", fake_request)
    monkeypatch.setattr(huawei_push, "_access_token", "")
    monkeypatch.setattr(huawei_push, "_access_token_expires_at", 0.0)

    result = asyncio.run(huawei_push.send_notification(
        ["push-token"], {"notification_id": "n1", "title": "消息", "body": "完成"},
    ))
    assert result.delivered is True
    assert calls[0][3] is True
    assert calls[1][2]["Authorization"] == "Bearer access-token"
