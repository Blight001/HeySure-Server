import os
import threading
from unittest.mock import patch

from connector_runtime.bots.qq import service
from connector_runtime.bots.qq import stream_sender


def _capture_stream_payload(monkeypatch):
    captured = []
    monkeypatch.setattr(service, "_load_qq_config", lambda *_: object())
    monkeypatch.setattr(service, "_message_endpoint", lambda *_: "https://qq.invalid/messages")
    monkeypatch.setattr(
        service,
        "_post_qq_message",
        lambda _cfg, *, endpoint, payload: captured.append(payload) or {"id": "stream-1"},
    )
    return captured


def test_qq_http_session_ignores_environment_proxy_settings():
    service._HTTP_LOCAL.session = None
    with patch.dict(
        os.environ,
        {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "ALL_PROXY": "socks5://127.0.0.1:7897",
        },
    ):
        session = service._qq_http_session()
        settings = session.merge_environment_settings(
            service.QQ_TOKEN_URL,
            {},
            None,
            None,
            None,
        )

    assert session.trust_env is False
    assert settings["proxies"] == {}


def test_first_stream_packet_omits_id_and_finishes_with_newline(monkeypatch):
    captured = _capture_stream_payload(monkeypatch)

    service.post_qq_stream_packet(
        1,
        2,
        text="hello",
        target_id="openid",
        target_type="c2c",
        stream_id="",
        stream_index=0,
        stream_state=10,
        msg_id="source-message",
        msg_seq=1,
    )

    payload = captured[0]
    assert "id" not in payload["stream"]
    assert payload["stream"]["index"] == 0
    assert payload["markdown"]["content"] == "hello\n"


def test_followup_stream_packet_reuses_server_id(monkeypatch):
    captured = _capture_stream_payload(monkeypatch)

    service.post_qq_stream_packet(
        1,
        2,
        text=" world",
        target_id="openid",
        target_type="c2c",
        stream_id="stream-1",
        stream_index=1,
        stream_state=1,
    )

    assert captured[0]["stream"]["id"] == "stream-1"
    assert captured[0]["markdown"]["content"] == " world"


def _bare_stream_session():
    stream = stream_sender.QQStreamSession.__new__(stream_sender.QQStreamSession)
    stream.user_id = 1
    stream.ai_config_id = 2
    stream.ai_kind = "core"
    stream.session_id = "qq-test"
    stream.target_id = "openid"
    stream.target_type = "c2c"
    stream.msg_id = "source-message"
    stream.event_id = ""
    stream.markdown_mode = "native"
    stream.template_id = ""
    stream._seq = 1
    stream._index = 0
    stream._lock = threading.Lock()
    stream._stream_id = ""
    stream._started = False
    stream._failed = False
    stream._last_sent_text = ""
    stream._last_text = ""
    stream._completed_text = ""
    stream._current_text = ""
    stream._done = False
    return stream


def test_failed_stream_packet_does_not_consume_passive_reply_sequence(monkeypatch):
    stream = _bare_stream_session()
    monkeypatch.setattr(
        stream_sender,
        "post_qq_stream_packet",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("rejected")),
    )

    stream._send_packet("hello", final=False)

    assert stream._failed is True
    assert stream._seq == 1
    assert stream._index == 0


def test_stream_sends_full_snapshots_and_reuses_passive_reply_sequence(monkeypatch):
    stream = _bare_stream_session()
    calls = []
    monkeypatch.setattr(
        stream_sender,
        "post_qq_stream_packet",
        lambda *args, **kwargs: calls.append(kwargs) or {"id": "stream-1"},
    )
    bumped = []
    monkeypatch.setattr(stream, "_bump_route_sequence", lambda next_seq: bumped.append(next_seq))

    stream._send_packet("hello", final=False)
    stream._send_packet("hello world", final=False)
    stream._send_packet("hello world", final=True, force=True)

    assert [call["text"] for call in calls] == ["hello", "hello world", "hello world"]
    assert [call["stream_index"] for call in calls] == [0, 1, 2]
    assert [call["msg_seq"] for call in calls] == [1, 1, 1]
    assert [call["reset"] for call in calls] == [False, True, True]
    assert calls[-1]["reset"] is True
    assert bumped == [2]


def test_stream_fallback_uses_next_sequence_after_partial_stream(monkeypatch):
    stream = _bare_stream_session()
    stream._started = True
    calls = []
    bumped = []
    monkeypatch.setattr(
        stream_sender,
        "send_qq_markdown_message",
        lambda *args, **kwargs: calls.append(kwargs) or {"message_id": "fallback"},
    )
    monkeypatch.setattr(stream, "_bump_route_sequence", lambda next_seq: bumped.append(next_seq))

    stream._fallback_full_send("complete answer")

    assert calls[0]["msg_seq"] == 2
    assert calls[0]["text"] == "complete answer"
    assert bumped == [3]


def test_finalize_bubble_closes_the_existing_run_stream(monkeypatch):
    stream = _bare_stream_session()
    stream._started = True
    stream._last_sent_text = "answer"
    calls = []
    monkeypatch.setattr(
        stream_sender,
        "post_qq_stream_packet",
        lambda *args, **kwargs: calls.append(kwargs) or {"id": "stream-1"},
    )

    stream._finalize_bubble("answer")

    assert calls[0]["text"] == "answer"
    assert calls[0]["stream_state"] == stream_sender._STREAM_STATE_FINISHED
    assert calls[0]["reset"] is True
    assert stream._started is True
    assert stream._stream_id == "stream-1"
    assert stream._index == 1
    assert stream._last_sent_text == "answer"
    assert stream._seq == 1


def test_update_merges_replayed_prefix_across_tool_turns(monkeypatch):
    """A tool boundary must not split a repeated sentence into two bubbles."""
    stream = _bare_stream_session()
    delivered = []

    def _capture_final(text, *, final, force=False):
        if final:
            delivered.append(text)
        stream._started = True
        stream._last_sent_text = text

    monkeypatch.setattr(stream, "_send_packet", _capture_final)
    monkeypatch.setattr(stream, "_fallback_full_send", lambda text: delivered.append(text))

    stream.update("阶段 4：先查询知识库与 Shadow 发布相关工具，")
    stream.update("")
    # The next model turn replays the prefix, then completes it.
    stream.update("阶段 4：先查询知识库与 Shadow 发布相关工具")
    assert stream._last_text == "阶段 4：先查询知识库与 Shadow 发布相关工具，"
    stream.update("阶段 4：先查询知识库与 Shadow 发布相关工具，再确认当前页面状态。")
    stream.update("")

    assert stream._last_text == "阶段 4：先查询知识库与 Shadow 发布相关工具，再确认当前页面状态。"
    stream._drain_on_close()
    assert delivered == ["阶段 4：先查询知识库与 Shadow 发布相关工具，再确认当前页面状态。"]


def test_update_keeps_distinct_tool_turns_in_one_bubble():
    stream = _bare_stream_session()

    stream.update("我先检查当前状态。")
    stream.update("")
    stream.update("检查完成，可以继续发布。")

    assert stream._last_text == "我先检查当前状态。\n\n检查完成，可以继续发布。"
