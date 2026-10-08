import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
import pytest

from bugbench import copilot_signalr as backend


def test_metrics_matches_captured_browser_shape():
    raw = backend._build_signalr_metrics_frame("hello", "conv-1", "chat-1", sequence=7)
    frame = json.loads(raw.rstrip(backend.RS))

    assert frame["type"] == 1
    assert frame["target"] == "Metrics"
    assert set(frame["arguments"][0]) == {"Timestamps"}
    timestamps = frame["arguments"][0]["Timestamps"]
    expected = {
        "ConnectionStart",
        "UserInputStart",
        "UserInputSubmit",
        "ConnectionEstablished",
        "RequestSent",
    }
    assert set(timestamps) == expected
    for value in timestamps.values():
        assert isinstance(value, str)
        datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_rebuilt_url_preserves_observed_variants_and_encodes_values():
    variants = "feature.One,feature.Two+bucket&flag=value"
    captured = (
        "wss://substrate.office.com/m365Copilot/Chathub/user@tenant"
        "?chatsessionid=old&X-SessionId=persistent&ConversationId=conv"
        "&access_token=tok%2Bwith%2Fchars&variants=" + quote(variants, safe="")
    )
    params = {
        "user_id": "user",
        "tenant_id": "tenant",
        "conversation_id": "conv",
        "access_token": "tok+with/chars",
        "ws_url": captured,
    }

    rebuilt = asyncio.run(backend._build_signalr_url(params, chat_session="fresh"))
    query = parse_qs(urlparse(rebuilt).query)

    assert query["variants"] == [variants]
    assert query["chatsessionid"] == ["fresh"]
    assert query["X-SessionId"] == ["persistent"]
    assert query["ConversationId"] == ["conv"]
    assert query["access_token"] == ["tok+with/chars"]


def test_missing_variants_is_not_invented():
    params = {
        "user_id": "user",
        "tenant_id": "tenant",
        "conversation_id": "conv",
        "access_token": "token",
    }
    rebuilt = asyncio.run(backend._build_signalr_url(params, chat_session="fresh"))
    assert "variants" not in parse_qs(urlparse(rebuilt).query)


def test_early_progress_is_ignored_but_real_delta_is_kept():
    early = {
        "type": 1,
        "target": "update",
        "arguments": [{
            "messages": [{"author": "bot", "contentType": "EarlyProgress", "text": "Putting it together…"}],
        }],
    }
    real = {
        "type": 1,
        "target": "update",
        "arguments": [{"writeAtCursor": "Verified answer", "isLastUpdate": True}],
    }

    early_text, _, _ = backend._extract_text_from_frame(early)
    real_text, is_final, _ = backend._extract_text_from_frame(real)

    assert early_text == ""
    assert real_text == "Verified answer"
    assert is_final is True
    assert backend._is_early_progress_text("Checking that now…") is True
    assert backend._is_early_progress_text("I'm working on it and here is the result") is False


def test_frame_log_rotates_and_stays_bounded(tmp_path):
    log_path = tmp_path / "signalr_frames.log"
    backend.SIGNALR_FRAME_LOG_PATH = str(log_path)
    backend.SIGNALR_FRAME_LOG_MAX_BYTES = 512

    for index in range(30):
        backend._append_signalr_frame_log({"frame": index, "data": "x" * 80})

    assert log_path.exists()
    assert log_path.stat().st_size <= 512
    assert (tmp_path / "signalr_frames.log.1").exists()
