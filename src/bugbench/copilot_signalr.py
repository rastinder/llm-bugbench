"""
Direct SignalR backend for M365 Copilot Chat.
Bypasses browser DOM scraping by connecting directly to the SignalR hub
at wss://substrate.office.com/m365Copilot/Chathub/...

Discovery: June 3, 2026
- M365 Chat uses SignalR over WebSocket (not raw WS)
- Endpoint: wss://substrate.office.com/m365Copilot/Chathub/{user_uuid}@{tenant_uuid}
- Requires access_token (JWT) from authenticated browser session
- Frames separated by ASCII 0x1E (record separator)
- Server sends type:1 (Invocation target="update") for partial text
- Server sends type:2 (StreamItem/Completion) for final text
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import re
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncGenerator
from typing import Any

try:
    import aiohttp
except ImportError:
    aiohttp = None

def token_fingerprint(token: str) -> str:
    return token[:8] if token else ""

def token_needs_refresh(token: str) -> bool:
    return False
def redact_sensitive(data: Any) -> Any:
    return data

def redact_text(text: str) -> str:
    return text

# SOCKS proxy support
try:
    from aiohttp_socks import ProxyConnector
    _HAS_SOCKS = True
except Exception:
    _HAS_SOCKS = False
    ProxyConnector = None  # type: ignore[misc,assignment]

COPILOT_SOCKS_PROXY = os.getenv("COPILOT_SOCKS_PROXY", "")
REQUEST_TIMEOUT = float(os.getenv("COPILOT_REQUEST_TIMEOUT", "120"))
CONNECT_TIMEOUT = float(os.getenv("COPILOT_CONNECT_TIMEOUT", "15"))

# Record separator used by SignalR JSON protocol
RS = "\x1e"

# The browser capture showed a very large frame stream during diagnostics. Keep
# the optional debug log bounded so repeated chats cannot fill the VPS disk.
SIGNALR_FRAME_LOG_PATH = os.getenv("COPILOT_SIGNALR_FRAME_LOG", "")
try:
    SIGNALR_FRAME_LOG_MAX_BYTES = int(os.getenv("COPILOT_SIGNALR_FRAME_LOG_MAX_BYTES", str(25 * 1024 * 1024)))
except ValueError:
    SIGNALR_FRAME_LOG_MAX_BYTES = 25 * 1024 * 1024
_SIGNALR_FRAME_LOG_LOCK = threading.Lock()


def _append_signalr_frame_log(frame: dict) -> None:
    """Append one diagnostic frame, rotating the log before it grows unbounded."""
    if not SIGNALR_FRAME_LOG_PATH:
        return
    try:
        line = json.dumps(redact_sensitive(frame), separators=(",", ":")) + "\n"
        max_bytes = max(0, SIGNALR_FRAME_LOG_MAX_BYTES)
        with _SIGNALR_FRAME_LOG_LOCK:
            if max_bytes and os.path.exists(SIGNALR_FRAME_LOG_PATH):
                if os.path.getsize(SIGNALR_FRAME_LOG_PATH) + len(line.encode("utf-8")) > max_bytes:
                    backup_path = SIGNALR_FRAME_LOG_PATH + ".1"
                    try:
                        os.replace(SIGNALR_FRAME_LOG_PATH, backup_path)
                    except FileNotFoundError:
                        pass
            with open(SIGNALR_FRAME_LOG_PATH, "a", encoding="utf-8") as frame_file:
                frame_file.write(line)
    except Exception:
        # Diagnostics must never affect a live chat response.
        pass



# ---------------------------------------------------------------------------
# Model and Tone Mapping
# ---------------------------------------------------------------------------

MODEL_TONE_MAP = {
    "auto": "Magic",
    "copilot": "Magic",
    "copilot-gpt": "Magic",
    "magic": "Magic",
    "gpt": "Magic",
    "copilot-quick": "Gpt_5_6_Chat",
    "quick response": "Gpt_5_6_Chat",
    "gpt-5.6": "Gpt_5_6_Chat",
    "gpt-5.6-quick": "Gpt_5_6_Chat",
    "gpt-5.6-chat": "Gpt_5_6_Chat",
    "copilot-think": "Gpt_5_6_Reasoning",
    "think deeper": "Gpt_5_6_Reasoning",
    "gpt-5.6-think": "Gpt_5_6_Reasoning",
    "gpt-5.6-think-deeper": "Gpt_5_6_Reasoning",
    "gpt-5.5": "Gpt_5_5_Chat",
    "gpt-5.5-chat": "Gpt_5_5_Chat",
    "gpt-5.5-think-deeper": "Gpt_5_5_Reasoning",
    "claude": "Claude_Sonnet",
    "claude-sonnet": "Claude_Sonnet",
    "claude-sonnet-4.6": "Claude_Sonnet",
    "claude-sonnet-5": "Claude_Sonnet",
    "claude-opus": "Claude_Opus",
    "claude-opus-5": "Claude_Opus",
    "gpt-6-think-deeper": "Gpt_6_Reasoning",
    "gpt-6-sol": "Gpt_6_Sol_Reasoning",
}

PAID_SCENARIO_TONES = frozenset({"Claude_Opus", "Gpt_6_Reasoning"})

# ---------------------------------------------------------------------------
# Configuration: optionsSets
# ---------------------------------------------------------------------------

# Base optionsSets that are safe to send on every turn.  Additional experimental
# flags can be appended via the COPILOT_SIGNALR_OPTIONSSETS env var.
DEFAULT_OPTIONS_SETS = [
    "search_result_progress_messages_with_search_queries",
    "update_textdoc_response_after_streaming",
    "deepleo_networking_timeout_10minutes_canmore",
    "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
    "gptvnorm2048",
    "localtime_inline_tunes",
    "dl_edge_tunes",
    "deepleo_history_meta",
    "deepleo_office_commerce_evidenced",
    "disable_emergency_assertion_generator",
    "enable_clarification_after_yes_no",
    "rc1",
    "mem",
    "memdt",
    "cwc_code_interpreter",
    "cwc_code_interpreter_amsfix",
    "cwc_code_interpreter_citation_fix",
    "code_interpreter_interactive_charts",
    "code_interpreter_matplotlib_patching",
    "ldqa",
    "ldsummary",
]

# User-facing override.  Comma-separated list of optionsSet names.  If the
# special value "default" is present, the default list above is kept and the
# rest are appended.
_OPTIONS_SETS_OVERRIDE = os.getenv("COPILOT_SIGNALR_OPTIONSSETS", "").strip()
if _OPTIONS_SETS_OVERRIDE:
    _extra = [s.strip() for s in _OPTIONS_SETS_OVERRIDE.split(",") if s.strip()]
    if "default" in _extra:
        _extra.remove("default")
        OPTIONS_SETS = list(DEFAULT_OPTIONS_SETS) + _extra
    else:
        OPTIONS_SETS = _extra
else:
    OPTIONS_SETS = list(DEFAULT_OPTIONS_SETS)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CopilotSignalRError(Exception):
    """Base exception for SignalR backend failures."""


class CopilotDisengagedError(CopilotSignalRError):
    """Raised when M365 returns a Disengaged/empty safety-filter response."""

    def __init__(self, message: str = "M365 Copilot disengaged", details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


# ---------------------------------------------------------------------------
# Image upload helpers
# ---------------------------------------------------------------------------

async def upload_image_to_m365(
    image_path: str,
    access_token: str,
    conversation_id: str,
    user_id: str,
    tenant_id: str,
    timeout: float = 60.0,
) -> dict | None:
    """Upload an image to M365 Copilot via the direct UploadFile API.

    Returns the JSON response from Microsoft, which includes the docId
    to reference in the SignalR chat payload. Returns None on failure.
    """
    from pathlib import Path

    image_path_obj = Path(image_path)
    if not image_path_obj.exists():
        print(f"[signalr-backend] Image file not found: {image_path}")
        return None
    ext = image_path_obj.suffix.lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    mime = f"image/{ext}"

    with open(image_path_obj, "rb") as f:
        image_bytes = f.read()
    b64_data = base64.b64encode(image_bytes).decode("utf-8")

    if not conversation_id:
        conversation_id = str(uuid.uuid4())
        print(f"[signalr-backend] No conversation_id available, generated {conversation_id}")

    boundary = "----WebKitFormBoundaryM365CopilotUpload"
    body_lines = [
        f"--{boundary}",
        'Content-Disposition: form-data; name="scenario"',
        "",
        "UploadImage",
        f"--{boundary}",
        'Content-Disposition: form-data; name="conversationId"',
        "",
        conversation_id,
        f"--{boundary}",
        'Content-Disposition: form-data; name="FileBase64"',
        "",
        f"data:{mime};base64,{b64_data}",
        f"--{boundary}",
        'Content-Disposition: form-data; name="optionsSets"',
        "",
        "cwcgptvsan",
        f"--{boundary}",
        'Content-Disposition: form-data; name="optionsSets"',
        "",
        "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
        f"--{boundary}",
        'Content-Disposition: form-data; name="optionsSets"',
        "",
        "gptvnorm2048",
        f"--{boundary}--",
        "",
    ]
    body = "\r\n".join(body_lines).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "x-anchormailbox": f"Oid:{user_id}@{tenant_id}",
        "x-scenario": "OfficeWebIncludedCopilot",
        "x-variants": "feature.EnableImageSupportInUploadFile",
        "origin": "https://m365.cloud.microsoft",
        "referer": "https://m365.cloud.microsoft/",
        "sec-ch-ua": "\"Chromium\";v=\"146\", \"Not-A.Brand\";v=\"24\", \"Google Chrome\";v=\"146\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
        "content-type": f"multipart/form-data; boundary={boundary}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }

    try:
        connector = _make_connector(limit=5)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(
                "https://substrate.office.com/m365Copilot/UploadFile",
                headers=headers,
                data=body,
            ) as resp:
                text = await resp.text()
                print(f"[signalr-backend] UploadFile status={resp.status}, len={len(text)}")
                if resp.status != 200:
                    print(f"[signalr-backend] UploadFile failed: {redact_text(text)[:500]}")
                    return None
                data = json.loads(text)
                print(f"[signalr-backend] UploadFile success: {data.get('fileName')} docId={data.get('docId')}")
                return data
    except Exception as e:
        print(f"[signalr-backend] UploadFile exception: {type(e).__name__}: {redact_text(str(e))}")
        return None


def _build_image_annotation(upload_result: dict, file_name: str | None = None) -> dict:
    """Build a messageAnnotation entry from an UploadFile response."""
    file_name = file_name or upload_result.get("fileName") or "uploaded_image.png"
    file_type = (upload_result.get("fileType") or ".png").lstrip(".").lower()
    if file_type == "jpg":
        file_type = "jpeg"
    return {
        "id": upload_result["docId"],
        "messageAnnotationMetadata": {
            "@type": "File",
            "annotationType": "File",
            "fileType": file_type,
            "fileName": file_name,
        },
        "messageAnnotationType": "ImageFile",
    }


# ---------------------------------------------------------------------------
# Connector + param extraction
# ---------------------------------------------------------------------------

def _make_connector(limit: int = 10) -> aiohttp.BaseConnector:
    """Create TCP or SOCKS connector depending on env config."""
    if COPILOT_SOCKS_PROXY and _HAS_SOCKS and ProxyConnector is not None:
        return ProxyConnector.from_url(COPILOT_SOCKS_PROXY, ssl=True, limit=limit)
    try:
        return aiohttp.TCPConnector(ssl=True, limit=limit, enable_cleanup_closed=True)
    except TypeError:
        return aiohttp.TCPConnector(ssl=True, limit=limit, enable_cleanup_closed=True)


async def _extract_signalr_params_from_page() -> dict | None:
    """Extract SignalR connection parameters from the CloakBrowser page.
    
    Priority:
    1. Playwright-native intercepted WS URL (most reliable — has live JWT)
    2. Page URL for conversation ID
    3. Cookies for token
    4. JS globals
    """
    try:
        from cloak_copilot_bridge import get_bridge
        bridge = await get_bridge()
        page = bridge._page
        if not page:
            return None

        current_url = page.url
        conversation_id = None
        token = None
        ws_url = None
        user_id = None
        tenant_id = None
        variants = None
        auth_status = bridge.get_auth_status() if hasattr(bridge, "get_auth_status") else {}

        # Strategy 1: Playwright-native intercepted WS URL (most reliable)
        intercepted_ws_url = bridge.get_last_ws_url()
        if intercepted_ws_url and "substrate" in intercepted_ws_url:
            ws_url = intercepted_ws_url
            # Parse query parameters from the WS URL
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(intercepted_ws_url)
            qs = parse_qs(parsed.query)
            token = qs.get("access_token", [None])[0]
            conversation_id = qs.get("ConversationId", [None])[0]
            variants = qs.get("variants", [None])[0]
            # Extract user@tenant from path
            path_match = re.search(r"/Chathub/([^/]+)", parsed.path)
            if path_match:
                user_tenant = path_match.group(1)
                if "@" in user_tenant:
                    user_id, tenant_id = user_tenant.split("@", 1)
            print(f"[signalr-backend] Extracted params from Playwright WS URL: conv={bool(conversation_id)}, token={bool(token)}, user={bool(user_id)}, tenant={bool(tenant_id)}")

        # Strategy 2: Extract conversation ID from page URL
        if not conversation_id:
            m = re.search(r"/conversation/([a-f0-9-]+)", current_url)
            if m:
                conversation_id = m.group(1)

        # Strategy 3: Extract token from cookies
        if not token:
            cookies = await page.context.cookies()
            for c in cookies:
                if c.get("name") == "OhpToken" and c.get("value"):
                    token = c["value"]
                    break

        # Strategy 4: Extract from page's JS globals
        if not user_id or not tenant_id:
            page_info = await page.evaluate("""() => {
                const result = { conversationId: null, userId: null, tenantId: null, wsUrl: null };
                const match = location.pathname.match(/\\/conversation\\/([a-f0-9-]+)/i);
                if (match) result.conversationId = match[1];
                if (window.__user) {
                    result.userId = window.__user.id || window.__user.oid;
                    result.tenantId = window.__user.tenantId;
                }
                if (window.__copilotWSUrl) result.wsUrl = window.__copilotWSUrl;
                for (const key of Object.keys(localStorage)) {
                    const val = localStorage.getItem(key);
                    if (val && val.includes('substrate') && val.includes('access_token')) {
                        result.wsUrl = val;
                        break;
                    }
                }
                return result;
            }""")
            if not conversation_id:
                conversation_id = page_info.get("conversationId")
            if not user_id:
                user_id = page_info.get("userId")
            if not tenant_id:
                tenant_id = page_info.get("tenantId")
            if not ws_url:
                ws_url = page_info.get("wsUrl")

        return {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "access_token": token,
            "ws_url": ws_url,
            "variants": variants,
            "current_url": current_url,
            "token_fingerprint": token_fingerprint(token),
            "token_generation": auth_status.get("generation"),
            "token_observed_at": auth_status.get("captured_at"),
            "token_source": auth_status.get("source"),
            "token_needs_refresh": auth_status.get("token_needs_refresh", False),
        }
    except Exception as e:
        print(f"[signalr-backend] Failed to extract params from page: {redact_text(str(e))}")
        return None


async def _extract_ws_url_from_page() -> str | None:
    """Try to extract the actual WebSocket URL from the page.
    
    Priority:
    1. Playwright-native intercepted WS URL (most reliable)
    2. JS-injected WS hook (window.__copilotWSUrls)
    3. Performance entries
    4. Global SignalR connection objects
    """
    try:
        from cloak_copilot_bridge import get_bridge
        bridge = await get_bridge()
        page = bridge._page
        if not page:
            return None

        # Strategy 1: Playwright-native intercepted WS URL (stored in bridge)
        ws_url = bridge.get_last_ws_url()
        if ws_url and "substrate" in ws_url and "Chathub" in ws_url:
            print(f"[signalr-backend] Using Playwright-intercepted WS URL: {redact_text(ws_url)[:80]}...")
            return ws_url

        # Strategy 2: Check if our injected WS hook captured the URL
        ws_url = await page.evaluate("""() => {
            if (window.__copilotWSUrls && window.__copilotWSUrls.length) {
                for (const url of window.__copilotWSUrls) {
                    if (url.includes('substrate') && url.includes('Chathub')) {
                        return url;
                    }
                }
            }
            return null;
        }""")
        if ws_url:
            print(f"[signalr-backend] Using JS hook WS URL: {redact_text(ws_url)[:80]}...")
            return ws_url

        # Strategy 3: Use performance entries
        ws_url = await page.evaluate("""() => {
            const entries = performance.getEntriesByType('resource');
            for (const e of entries) {
                if (e.name && e.name.includes('substrate') && e.name.includes('Chathub')) {
                    return e.name;
                }
            }
            return null;
        }""")
        if ws_url:
            print(f"[signalr-backend] Using performance entry WS URL: {redact_text(ws_url)[:80]}...")
            return ws_url

        # Strategy 4: Fallback to finding SignalR connection objects
        ws_url = await page.evaluate("""() => {
            for (const key of Object.keys(window)) {
                const obj = window[key];
                if (obj && typeof obj === 'object') {
                    if (obj.connectionId || obj.hubUrl || obj.url) {
                        const url = obj.hubUrl || obj.url || obj.baseUrl;
                        if (url && url.includes('substrate')) return url;
                    }
                }
            }
            return null;
        }""")
        if ws_url:
            print(f"[signalr-backend] Using global object WS URL: {redact_text(ws_url)[:80]}...")
        return ws_url
    except Exception as e:
        print(f"[signalr-backend] Failed to extract WS URL: {redact_text(str(e))}")

        return None


def _fresh_one_shot_params(params: dict) -> dict:
    fresh = dict(params)
    chat_session = str(uuid.uuid4())
    fresh["conversation_id"] = str(uuid.uuid4())
    fresh["x_session_id"] = chat_session
    fresh["chatsessionid"] = chat_session
    return fresh


async def _build_signalr_url(params: dict, chat_session: str | None = None) -> str | None:
    """Build the SignalR WebSocket URL from extracted parameters.

    We NEVER reuse the exact intercepted URL because the chatsessionid
    embedded in it is single-use.  Instead we parse the intercepted URL
    (or page) for the persistent bits (token, conversation, user, tenant,
    X-SessionId) and generate a fresh chatsessionid for each prompt.
    """
    user_id = params.get("user_id")
    tenant_id = params.get("tenant_id")
    token = params.get("access_token")
    conversation_id = params.get("conversation_id")
    x_session_id = params.get("x_session_id")
    variants = params.get("variants")
    agent_kind = params.get("agent")
    gpt_id = params.get("gpt_id")

    # 1) Try to get persistent params from the intercepted WS URL
    ws_url = params.get("ws_url") or await _extract_ws_url_from_page()
    if ws_url:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(ws_url)
        qs = parse_qs(parsed.query)
        token = token or qs.get("access_token", [None])[0]
        conversation_id = conversation_id or qs.get("ConversationId", [None])[0]
        x_session_id = x_session_id or qs.get("X-SessionId", [None])[0]
        variants = variants or qs.get("variants", [None])[0]
        # Agent routing bits.  Base chat sends agent=web with no gptId; an
        # agent conversation sends agent=Agent plus the agent's gptId.  Both
        # are propagated verbatim so the Chathub routes the turn to the right
        # agent.  Absent -> base-chat query is byte-identical to before.
        agent_kind = agent_kind or qs.get("agent", [None])[0]
        gpt_id = gpt_id or qs.get("gptId", [None])[0]
        # user@tenant from path if we didn't already have it
        if not user_id or not tenant_id:
            m = re.search(r"/Chathub/([^/]+)", parsed.path)
            if m and "@" in m.group(1):
                user_id, tenant_id = m.group(1).split("@", 1)

    if not all([user_id, tenant_id, token, conversation_id]):
        print(
            f"[signalr-backend] Missing required params: "
            f"user={bool(user_id)}, tenant={bool(tenant_id)}, "
            f"token={bool(token)}, conv={bool(conversation_id)}"
        )
        return None

    # Generate fresh per-prompt session id, but keep the persistent X-SessionId.
    # urlencode is important here because both JWTs and browser variants can
    # contain characters that must not be interpreted as query delimiters.
    from urllib.parse import urlencode
    chat_session = chat_session or params.get("chatsessionid") or str(uuid.uuid4())
    browser_session = x_session_id or chat_session
    tone = params.get("tone") or "Magic"
    is_paid = tone in PAID_SCENARIO_TONES or params.get("scenario") == "OfficeWebPaidCopilot"
    scenario = "OfficeWebPaidCopilot" if is_paid else "OfficeWebIncludedCopilot"
    license_type = "Premium" if is_paid else "Starter"

    query = {
        "chatsessionid": chat_session,
        "XRoutingParameterSessionKey": chat_session,
        "clientrequestid": chat_session,
        "X-SessionId": browser_session,
        "ConversationId": conversation_id,
        "access_token": token,
        "source": "officeweb",
        "product": "Office",
        "agentHost": "Bizchat.FullScreen",
        "licenseType": license_type,
        "isEdu": "false",
        "agent": agent_kind or "web",
        "scenario": scenario,
        "disableMemory": "1",
    }
    if variants:
        query["variants"] = variants
    if gpt_id:
        query["gptId"] = gpt_id
    return (
        f"wss://substrate.office.com/m365Copilot/Chathub/{user_id}@{tenant_id}"
        f"?{urlencode(query)}"
    )


# ---------------------------------------------------------------------------
# SignalR frame builders
# ---------------------------------------------------------------------------

def _build_signalr_handshake() -> str:
    """Build SignalR JSON protocol handshake message."""
    return json.dumps({"protocol": "json", "version": 1}) + RS


def _build_signalr_stream_invocation(invocation_id: str, target: str, arguments: list) -> str:
    """Build a SignalR StreamInvocation message (type 4)."""
    return json.dumps({
        "type": 4,
        "invocationId": invocation_id,
        "target": target,
        "arguments": arguments,
    }) + RS


def _build_signalr_ping() -> str:
    """Build a SignalR Ping message (type 6)."""
    return json.dumps({"type": 6}) + RS


def _build_signalr_metrics_frame(
    prompt: str,
    conversation_id: str,
    chat_session_id: str,
    sequence: int = 0,
) -> str:
    """Build the Metrics invocation frame observed in browser captures.

    Sending this frame alongside the chat invocation appears to improve
    reliability for multi-turn and tool-heavy conversations.
    """
    # The browser sends ISO-8601 UTC telemetry under this exact object. Keep
    # the public signature for callers, but do not invent extra metric fields.
    now = datetime.now(timezone.utc)
    def stamp(offset_ms: int) -> str:
        return (now - timedelta(milliseconds=offset_ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    payload = {
        "type": 1,
        "target": "Metrics",
        "arguments": [{
            "Timestamps": {
                "ConnectionStart": stamp(100),
                "UserInputStart": stamp(80),
                "UserInputSubmit": stamp(20),
                "ConnectionEstablished": stamp(10),
                "RequestSent": stamp(0),
            }
        }],
    }
    return json.dumps(payload) + RS


# ---------------------------------------------------------------------------
# Chat payload builder
# ---------------------------------------------------------------------------

def _build_chat_payload(
    prompt: str,
    session_id: str,
    trace_id: str,
    is_start: bool = False,
    template: dict | None = None,
    image_annotations: list[dict] | None = None,
    options_sets: list[str] | None = None,
    conversation_history: list[dict[str, Any]] | None = None,
    tone: str | None = None,
) -> dict:
    """Build the M365 Copilot chat message payload matching the browser format.

    If a captured browser template is provided, we deep-copy it and swap only
    the message text and correlation IDs. This guarantees payload fidelity.
    """
    if template is not None:
        payload = copy.deepcopy(template)
        # Update session/correlation IDs to match the current WS URL
        payload["sessionId"] = session_id
        payload["clientCorrelationId"] = trace_id
        payload["traceId"] = trace_id
        if "clientInfo" in payload and isinstance(payload["clientInfo"], dict):
            payload["clientInfo"]["clientSessionId"] = session_id
        if "message" in payload and isinstance(payload["message"], dict):
            payload["message"]["text"] = prompt
            payload["message"]["requestId"] = trace_id
            if image_annotations:
                payload["message"]["messageAnnotations"] = image_annotations
        return payload

    options_sets = options_sets or OPTIONS_SETS

    # For delta sends we may want to ship only the latest user message, but
    # M365 still expects the full options/context envelope.  We keep the
    # envelope identical and only vary the message text / history.
    message = {
        "author": "user",
        "inputMethod": "Keyboard",
        "text": prompt,
        "entityAnnotationTypes": ["People", "File", "Event", "Email", "TeamsMessage"],
        "requestId": trace_id,
        "locationInfo": {
            "timeZoneOffset": -4,
            "timeZone": "America/Toronto",
        },
        "locale": "en-us",
        "messageType": "Chat",
        "experienceType": "Default",
        "adaptiveCards": [],
        "clientPreferences": {},
        "connectedFederatedConnections": ["dummyid"],
        "messageAnnotations": image_annotations or [],
    }

    # If we have a structured conversation history, embed it so the server sees
    # the prior turns rather than a single flattened prompt.  The server still
    # receives the current prompt as the last message.
    if conversation_history:
        message["previousMessages"] = conversation_history[-20:]

    return {
        "source": "officeweb",
        "clientCorrelationId": trace_id,
        "sessionId": session_id,
        "optionsSets": options_sets,
        "streamingMode": "ConciseWithPadding",
        "spokenTextMode": "None",
        "options": {},
        "extraExtensionParameters": {},
        "allowedMessageTypes": [
            "Chat", "Suggestion", "InternalSearchQuery", "Disengaged",
            "InternalLoaderMessage", "Progress", "GeneratedCode",
            "RenderCardRequest", "AdsQuery", "SemanticSerp",
            "GenerateContentQuery", "GenerateGraphicArt", "SearchQuery",
            "ConfirmationCard", "AuthError", "DeveloperLogs",
            "TriggerPlugin", "HintInvocation", "MemoryUpdate",
            "EndOfRequest", "TriggerConfirmation", "ResumeInvokeAction",
            "ResumeUserInputRequest", "TriggerUserInputRequest",
            "EscapeHatch", "TriggerPluginAuth", "ResumePluginAuth",
            "SideBySide", "ReferencesListComplete", "SwitchRespondingEndpoint",
        ],
        "sliceIds": [],
        "threadLevelGptId": {},
        "traceId": trace_id,
        "isStartOfSession": is_start,
        "clientInfo": {
            "clientPlatform": "mcmcopilot-web",
            "clientAppName": "Office",
            "clientEntrypoint": "mcmcopilot-officeweb",
            "clientSessionId": session_id,
            "ProductCategory": "Chat",
            "clientAppType": "Web",
            "productEntryPoint": "ChatPanel",
            "deviceOS": "Windows",
            "deviceType": "Desktop",
            "clientPlatformVersion": "10",
        },
        "message": message,
        "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
        "isSbsSupported": True,
        "tone": MODEL_TONE_MAP.get(str(tone).lower().strip(), tone) if tone else "Magic",
        "renderReferencesBehindEOS": True,
        "disconnectBehavior": "continue",
    }# ---------------------------------------------------------------------------
# Response frame parser
# ---------------------------------------------------------------------------

# The browser emits transient EarlyProgress messages before real content. They
# are useful to the UI but must not become the API's final answer.
EARLY_PROGRESS_TEXT_MARKERS = (
    "getting things ready",
    "lining things up",
    "putting it together",
    "taking a look",
    "queuing things up",
    "hang on a sec",
    "just a sec",
    "just a second",
    "digging in",
    "making it happen",
    "working on it",
    "just a moment",
    "let me think",
    "searching for",
    "checking things",
    "checking that now",
)


def _is_early_progress_text(text: str) -> bool:
    if not text:
        return False
    normalized = re.sub(r"\s+", " ", text.lower()).strip().rstrip(".…\u2026").strip()
    return normalized in EARLY_PROGRESS_TEXT_MARKERS


# Transcript / UI chrome message types that must never become the assistant's
# answer text. The live protocol omits messageType on real answer frames (and
# uses "Chat" for consolidated replies), so this is deliberately a blocklist:
# unknown future message types stay accepted rather than being silently dropped.
NON_ANSWER_MESSAGE_TYPES = (
    "progress",
    "generatedcode",
    "referenceslistcomplete",
    "suggestion",
    "hintinvocation",
    "internalloadermessage",
    "triggerplugin",
    "resumeinvokeaction",
    "confirmationcard",
)


def _is_non_answer_message(msg_type: Any) -> bool:
    """True when a messages[] entry is transcript chrome, not answer text."""
    return str(msg_type or "").strip().lower() in NON_ANSWER_MESSAGE_TYPES



# C7 — correlation-id filtering: two concurrent requests share one M365
# conversation, and the old loop accepted the text of ANY frame that arrived
# ("if chunk_text: response_text = chunk_text"), so a peer's answer could be
# read as ours. Frames that explicitly claim a foreign requestId are skipped.
SIGNALR_FILTER_BY_REQUEST = (
    os.getenv("COPILOT_SIGNALR_FILTER_BY_REQUEST", "1").strip().lower()
    not in ("0", "false", "no")
)


def _frame_request_ids(frame: Any) -> set[str]:
    """Request ids a frame explicitly claims (shallow scan of arguments/item)."""
    ids: set[str] = set()
    if not isinstance(frame, dict):
        return ids
    args = frame.get("arguments")
    if isinstance(args, list):
        for arg in args:
            if not isinstance(arg, dict):
                continue
            rid = arg.get("requestId")
            if isinstance(rid, str) and rid:
                ids.add(rid)
            messages = arg.get("messages")
            if isinstance(messages, list):
                for m in messages:
                    if isinstance(m, dict):
                        mrid = m.get("requestId")
                        if isinstance(mrid, str) and mrid:
                            ids.add(mrid)
    item = frame.get("item")
    if isinstance(item, dict):
        rid = item.get("requestId")
        if isinstance(rid, str) and rid:
            ids.add(rid)
    top = frame.get("requestId")
    if isinstance(top, str) and top:
        ids.add(top)
    return ids


def _extract_text_from_frame(frame: dict) -> tuple[str, bool, dict]:
    """Extract assistant text and completion state from a SignalR frame.

    Returns (text, is_final, meta) where meta may contain failure details.
    """
    sr_type = frame.get("type")
    target = frame.get("target", "")
    text = ""
    is_final = False
    meta: dict[str, Any] = {}

    if sr_type == 1 and target == "update":
        args = frame.get("arguments", [])
        for arg in args:
            if not arg or not isinstance(arg, dict):
                continue
            write_text = arg.get("writeAtCursor", "")
            if write_text and not _is_early_progress_text(write_text):
                text += write_text
            for m in arg.get("messages", []):
                if not m or not isinstance(m, dict):
                    continue
                if m.get("author") == "user":
                    continue
                if m.get("contentType") == "EarlyProgress":
                    continue
                msg_text = m.get("text", "")
                msg_type = m.get("messageType", "")
                if msg_type == "Disengaged":
                    meta["disengaged"] = True
                    meta["disengaged_reason"] = m.get("hiddenText", "")
                if (
                    msg_text
                    and not _is_early_progress_text(msg_text)
                    and not _is_non_answer_message(msg_type)
                ):
                    text = msg_text
            if arg.get("isLastUpdate"):
                is_final = True

    elif sr_type == 2:  # StreamItem / Completion
        item = frame.get("item", {})
        result = item.get("result", {})
        if result.get("value") == "InvalidRequest":
            text = f"(InvalidRequest: {result.get('message', 'unknown')})"
            meta["invalid_request"] = True
            is_final = True
        else:
            messages = item.get("messages", [])
            for m in messages:
                if not m or not isinstance(m, dict):
                    continue
                if m.get("author") == "user":
                    continue
                if m.get("contentType") == "EarlyProgress":
                    continue
                msg_text = m.get("text", "")
                msg_type = m.get("messageType", "")
                if msg_type == "Disengaged":
                    meta["disengaged"] = True
                    meta["disengaged_reason"] = m.get("hiddenText", "")
                if (
                    msg_text
                    and not _is_early_progress_text(msg_text)
                    and not _is_non_answer_message(msg_type)
                ):
                    text = msg_text
            is_final = True

    elif sr_type in (3, 7):  # Close signals
        is_final = True

    elif sr_type == 5:  # Invocation binding failure
        meta["binding_failure"] = frame.get("error", "unknown")
        is_final = True

    return text, is_final, meta


def _raise_if_disengaged(text: str, meta: dict) -> None:
    """Raise CopilotDisengagedError if the response indicates disengagement."""
    if meta.get("disengaged"):
        raise CopilotDisengagedError(
            message=meta.get("disengaged_reason") or "M365 Copilot disengaged",
            details=meta,
        )
    # Also treat empty text on a final frame as a soft disengagement when the
    # caller explicitly expects content.
    if not text or text.strip() == "":
        # Don't raise here; let the caller decide.  But if combined with other
        # disengagement markers we already raised above.
        pass


# ---------------------------------------------------------------------------
# Low-level chat completion
# ---------------------------------------------------------------------------

async def _signalr_chat_completion(
    prompt: str,
    ws_url: str,
    timeout: float = 120.0,
    image_paths: list[str] | None = None,
    params: dict | None = None,
    is_start: bool = True,
    conversation_history: list[dict[str, Any]] | None = None,
    sequence: int = 0,
) -> str:
    """Connect to M365 SignalR hub and send a prompt.

    Returns the assistant's response text.
    """
    connector = _make_connector()
    timeout_obj = aiohttp.ClientTimeout(total=timeout + 30, sock_connect=CONNECT_TIMEOUT)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout_obj) as session:
        async with session.ws_connect(
            ws_url,
            protocols=["json"],
            heartbeat=30.0,
            headers={
                "Origin": "https://m365.cloud.microsoft",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
            },
        ) as ws:
            print(f"[signalr-backend] Connected to {redact_text(ws_url)[:80]}...")

            # 1) Send SignalR handshake
            await ws.send_str(_build_signalr_handshake())

            # 2) Wait for handshake response
            handshake_ok = False
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if msg.data.strip() in ("{}", ""):
                        handshake_ok = True
                        break
                    if RS in msg.data:
                        for part in msg.data.split(RS):
                            if part.strip() == "{}":
                                handshake_ok = True
                                break
                        if handshake_ok:
                            break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(f"SignalR handshake error: {ws.exception()}")
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    raise RuntimeError("SignalR connection closed during handshake")

            if not handshake_ok:
                print("[signalr-backend] Warning: handshake response unclear, proceeding anyway")

            # 3) Send ping (browser does this after handshake)
            await ws.send_str(_build_signalr_ping())

            # 4) Extract session params from WS URL (MUST match for server validation)
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(ws_url)
            qs = parse_qs(parsed.query)
            session_id = qs.get("X-SessionId", [str(uuid.uuid4())])[0]
            client_corr_id = qs.get("chatsessionid", [str(uuid.uuid4())])[0]
            conversation_id = qs.get("ConversationId", [str(uuid.uuid4())])[0]
            print(f"[signalr-backend] WS URL query: X-SessionId={session_id}, chatsessionid={client_corr_id}")

            # 5) Build and send chat StreamInvocation + Metrics frame
            image_annotations = []
            if image_paths and params:
                access_token = params.get("access_token")
                conversation_id_img = params.get("conversation_id") or conversation_id
                user_id = params.get("user_id")
                tenant_id = params.get("tenant_id")
                if access_token and conversation_id_img and user_id and tenant_id:
                    for img_path in image_paths:
                        upload_result = await upload_image_to_m365(
                            img_path, access_token, conversation_id_img, user_id, tenant_id
                        )
                        if upload_result:
                            image_annotations.append(_build_image_annotation(upload_result))
            chat_payload = _build_chat_payload(
                prompt, session_id, client_corr_id, is_start=is_start,
                image_annotations=image_annotations if image_annotations else None,
                conversation_history=conversation_history,
            )
            chat_msg = _build_signalr_stream_invocation("0", "chat", [chat_payload])
            metrics_msg = _build_signalr_metrics_frame(
                prompt=prompt,
                conversation_id=conversation_id,
                chat_session_id=client_corr_id,
                sequence=sequence,
            )
            combined = chat_msg + metrics_msg
            print(f"[signalr-backend] Sending chat StreamInvocation + Metrics ({len(combined)} chars)")
            await ws.send_str(combined)

            # 6) Collect streaming response
            response_text = ""
            ready = False
            frame_log_count = 0

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = msg.data
                    for part in data.split(RS):
                        p = part.strip()
                        if not p:
                            continue
                        try:
                            frame = json.loads(p)
                        except json.JSONDecodeError:
                            continue

                        frame_log_count += 1
                        if SIGNALR_FILTER_BY_REQUEST:
                            _fids = _frame_request_ids(frame)
                            if _fids and client_corr_id not in _fids:
                                continue  # foreign request's frame on the shared conversation
                        sr_type = frame.get("type")
                        target = frame.get("target", "")

                        # Log ALL frames for debugging (first 15 only)
                        if frame_log_count <= 15:
                            print(f"[signalr-backend] Frame #{frame_log_count} type={sr_type} target={target}")
                        # Dump full frames to file for image upload debugging
                        try:
                            _append_signalr_frame_log(frame)
                        except Exception:
                            pass

                        chunk_text, is_final, meta = _extract_text_from_frame(frame)
                        if chunk_text:
                            response_text = chunk_text

                        _raise_if_disengaged(response_text, meta)

                        if meta.get("invalid_request") or meta.get("binding_failure"):
                            ready = True
                            break

                        if is_final:
                            ready = True
                            break

                        if sr_type in (3, 7):  # Close signals
                            ready = True
                            break

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(f"SignalR error: {ws.exception()}")
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    ready = True
                    break

                if ready:
                    break

            return response_text or "(no response from SignalR)"


async def _signalr_chat_completion_stream(
    prompt: str,
    ws_url: str,
    timeout: float = 120.0,
    is_start: bool = True,
    conversation_history: list[dict[str, Any]] | None = None,
    sequence: int = 0,
) -> AsyncGenerator[str, None]:
    """Connect to M365 SignalR hub and stream a prompt's response chunks.

    Yields the assistant's response text chunks as they arrive.
    """
    connector = _make_connector()
    timeout_obj = aiohttp.ClientTimeout(total=timeout + 30, sock_connect=CONNECT_TIMEOUT)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout_obj) as session:
        async with session.ws_connect(
            ws_url,
            protocols=["json"],
            heartbeat=30.0,
            headers={
                "Origin": "https://m365.cloud.microsoft",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
            },
        ) as ws:
            print(f"[signalr-backend] Connected for streaming to {redact_text(ws_url)[:80]}...")

            # 1) Send SignalR handshake
            await ws.send_str(_build_signalr_handshake())

            # 2) Wait for handshake response
            handshake_ok = False
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if msg.data.strip() in ("{}", ""):
                        handshake_ok = True
                        break
                    if RS in msg.data:
                        for part in msg.data.split(RS):
                            if part.strip() == "{}":
                                handshake_ok = True
                                break
                        if handshake_ok:
                            break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(f"SignalR handshake error: {ws.exception()}")
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    raise RuntimeError("SignalR connection closed during handshake")

            if not handshake_ok:
                print("[signalr-backend] Warning: handshake response unclear, proceeding anyway")

            # 3) Send ping
            await ws.send_str(_build_signalr_ping())

            # 4) Extract session params from WS URL
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(ws_url)
            qs = parse_qs(parsed.query)
            session_id = qs.get("X-SessionId", [str(uuid.uuid4())])[0]
            client_corr_id = qs.get("chatsessionid", [str(uuid.uuid4())])[0]
            conversation_id = qs.get("ConversationId", [str(uuid.uuid4())])[0]

            # 5) Send chat StreamInvocation + Metrics frame
            chat_payload = _build_chat_payload(
                prompt, session_id, client_corr_id, is_start=is_start,
                conversation_history=conversation_history,
            )
            chat_msg = _build_signalr_stream_invocation("0", "chat", [chat_payload])
            metrics_msg = _build_signalr_metrics_frame(
                prompt=prompt,
                conversation_id=conversation_id,
                chat_session_id=client_corr_id,
                sequence=sequence,
            )
            await ws.send_str(chat_msg + metrics_msg)

            # 6) Collect streaming response
            response_text = ""
            yielded_text = ""
            ready = False
            frame_log_count = 0

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = msg.data
                    for part in data.split(RS):
                        p = part.strip()
                        if not p:
                            continue
                        try:
                            frame = json.loads(p)
                        except json.JSONDecodeError:
                            continue

                        frame_log_count += 1
                        if SIGNALR_FILTER_BY_REQUEST:
                            _fids = _frame_request_ids(frame)
                            if _fids and client_corr_id not in _fids:
                                continue  # foreign request's frame on the shared conversation

                        chunk_text, is_final, meta = _extract_text_from_frame(frame)
                        if chunk_text:
                            response_text = chunk_text

                        _raise_if_disengaged(response_text, meta)

                        if meta.get("invalid_request") or meta.get("binding_failure"):
                            ready = True
                            break

                        # Yield new content delta
                        if response_text.startswith(yielded_text):
                            delta = response_text[len(yielded_text):]
                            if delta:
                                yield delta
                                yielded_text = response_text
                        else:
                            # If text changed entirely, yield the new full text
                            yield response_text
                            yielded_text = response_text

                        if is_final:
                            ready = True
                            break

                        if sr_type := frame.get("type") in (3, 7):
                            ready = True
                            break

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(f"SignalR error: {ws.exception()}")
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    ready = True
                    break

                if ready:
                    break


# ---------------------------------------------------------------------------
# Reusable chat session (conversation reuse / delta sends)
# ---------------------------------------------------------------------------

class SignalRChatSession:
    """Reusable SignalR session for multi-turn conversation.

    Extracts connection parameters once and reuses ConversationId + X-SessionId
    across turns.  Each turn still opens a fresh WebSocket (M365 closes the hub
    after each response), but the persistent identifiers stay the same so the
    server sees a continuous conversation.
    """

    def __init__(self, model_id: str = "copilot", timeout: float = 120.0):
        self.model_id = model_id
        self.timeout = timeout
        self._params: dict | None = None
        self._ws_url: str | None = None
        self._x_session_id: str | None = None
        self._conversation_id: str | None = None
        self._token_generation: int | None = None
        self._token_fingerprint: str | None = None
        self._history: list[dict[str, Any]] = []
        self._turn_count = 0
        self._closed = False
        # One SignalR conversation is sequential. Concurrent turns would reuse
        # the same ConversationId/X-SessionId and interleave frames/history.
        self._lock = asyncio.Lock()

    async def _ensure_params(self) -> dict:
        current = await _extract_signalr_params_from_page()
        if not current or not current.get("ws_url"):
            raise RuntimeError(
                "No SignalR WS URL captured yet — first prompt must go through CloakBrowser"
            )
        generation = current.get("token_generation")
        fingerprint = current.get("token_fingerprint")
        if self._params is None:
            self._params = current
        elif generation is not None and generation != self._token_generation:
            self._params = current
            if fingerprint != self._token_fingerprint:
                self._ws_url = None
                self._x_session_id = current.get("x_session_id") or self._x_session_id
                self._conversation_id = current.get("conversation_id") or self._conversation_id
        self._token_generation = generation
        self._token_fingerprint = fingerprint
        return self._params

    def invalidate_auth(self) -> None:
        self._params = None
        self._ws_url = None
        self._token_generation = None
        self._token_fingerprint = None

    async def _ensure_url(self) -> str:
        if self._ws_url is None:
            params = await self._ensure_params()
            self._ws_url = await _build_signalr_url(params)
            if not self._ws_url:
                raise RuntimeError("Could not build SignalR WebSocket URL")
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self._ws_url)
            qs = parse_qs(parsed.query)
            self._x_session_id = qs.get("X-SessionId", [None])[0]
            self._conversation_id = qs.get("ConversationId", [None])[0]
        return self._ws_url

    async def send(self, prompt: str) -> str:
        """Send one serialized user turn and return the assistant response."""
        async with self._lock:
            if self._closed:
                raise CopilotSignalRError("SignalRChatSession is closed")

            ws_url = await self._ensure_url()
            is_start = self._turn_count == 0

            # Reuse the same X-SessionId and ConversationId across turns, but
            # generate a fresh chatsessionid per turn so we rebuild the WS URL.
            params = await self._ensure_params()
            params = dict(params)
            params["x_session_id"] = self._x_session_id
            params["conversation_id"] = self._conversation_id
            # Pass the original captured WS URL so _build_signalr_url does not
            # need to re-extract it from the browser page on every turn.
            params["ws_url"] = self._ws_url

            ws_url = await _build_signalr_url(params)
            response = await _signalr_chat_completion(
                prompt=prompt,
                ws_url=ws_url,
                timeout=self.timeout,
                params=params,
                is_start=is_start,
                conversation_history=self._history,
                sequence=self._turn_count,
            )

            # Record turn in history. We store lightweight role/text only; the
            # server is authoritative, but this helps us build delta context.
            self._history.append({"author": "user", "text": prompt})
            self._history.append({"author": "assistant", "text": response})
            if len(self._history) > 40:
                self._history = self._history[-40:]

            self._turn_count += 1
            return response

    async def send_stream(self, prompt: str) -> AsyncGenerator[str, None]:
        """Send one serialized user turn and yield response chunks."""
        async with self._lock:
            if self._closed:
                raise CopilotSignalRError("SignalRChatSession is closed")

            ws_url = await self._ensure_url()
            is_start = self._turn_count == 0
            params = await self._ensure_params()
            params = dict(params)
            params["x_session_id"] = self._x_session_id
            params["conversation_id"] = self._conversation_id
            params["ws_url"] = self._ws_url
            ws_url = await _build_signalr_url(params)

            response_chunks: list[str] = []
            try:
                async for chunk in _signalr_chat_completion_stream(
                    prompt=prompt,
                    ws_url=ws_url,
                    timeout=self.timeout,
                    is_start=is_start,
                    conversation_history=self._history,
                    sequence=self._turn_count,
                ):
                    response_chunks.append(chunk)
                    yield chunk
            except (asyncio.CancelledError, GeneratorExit):
                # An interrupted turn must not be recorded as a completed
                # assistant response or advance the conversation sequence.
                raise
            else:
                response = "".join(response_chunks)
                self._history.append({"author": "user", "text": prompt})
                self._history.append({"author": "assistant", "text": response})
                if len(self._history) > 40:
                    self._history = self._history[-40:]
                self._turn_count += 1

    def close(self) -> None:
        # close() is synchronous for existing callers; the event loop only
        # observes this boolean between serialized turns.
        self._closed = True

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id


async def signalr_chat_session(
    model_id: str = "copilot",
    timeout: float = 120.0,
) -> SignalRChatSession:
    """Create a reusable SignalR chat session."""
    session = SignalRChatSession(model_id=model_id, timeout=timeout)
    await session._ensure_url()
    return session


async def signalr_chat_completion_with_session(
    prompt: str,
    session: SignalRChatSession,
) -> str:
    """Send a prompt through a reusable SignalR session."""
    return await session.send(prompt)


# ---------------------------------------------------------------------------
# Public one-shot entry points
# ---------------------------------------------------------------------------

async def signalr_chat_completion_stream(
    prompt: str,
    model_id: str = "copilot",
    timeout: float = 120.0,
) -> AsyncGenerator[str, None]:
    """Main entry point for streaming: extract params from CloakBrowser and stream via SignalR."""
    print(f"[signalr-backend] Starting SignalR streaming for prompt ({len(prompt)} chars)")

    params = await _extract_signalr_params_from_page()

    if not params or not params.get("ws_url"):
        raise RuntimeError(
            "No SignalR WS URL captured yet — first prompt must go through CloakBrowser"
        )

    params = _fresh_one_shot_params(params)
    params["model_id"] = model_id
    params["tone"] = MODEL_TONE_MAP.get(str(model_id).lower().strip(), "Magic")
    ws_url = await _build_signalr_url(params)
    if not ws_url:
        raise RuntimeError("Could not build SignalR WebSocket URL")

    async for chunk in _signalr_chat_completion_stream(prompt, ws_url, timeout):
        yield chunk


async def signalr_chat_completion(
    prompt: str,
    model_id: str = "copilot",
    timeout: float = 120.0,
    image_paths: list[str] | None = None,
) -> str:
    """Main entry point: extract params from CloakBrowser and chat via SignalR.

    The first prompt MUST go through CloakBrowser so that Playwright WS
    interception captures the live SignalR WS URL + JWT. Each one-shot call
    uses a fresh conversation and browser session. SignalRChatSession remains
    available for intentional multi-turn reuse.
    """
    print(f"[signalr-backend] Starting SignalR chat for prompt ({len(prompt)} chars)")

    params = await _extract_signalr_params_from_page()

    if not params or not params.get("ws_url"):
        raise RuntimeError(
            "No SignalR WS URL captured yet — first prompt must go through CloakBrowser"
        )

    params = _fresh_one_shot_params(params)
    params["model_id"] = model_id
    params["tone"] = MODEL_TONE_MAP.get(str(model_id).lower().strip(), "Magic")

    if image_paths:
        print(
            f"[signalr-backend] Using disposable conversation for image request: "
            f"{params.get('conversation_id')}"
        )

    print(
        f"[signalr-backend] Extracted params: "
        f"conv={params.get('conversation_id')}, "
        f"user={params.get('user_id')}, "
        f"tenant={params.get('tenant_id')}, "
        f"token={'yes' if params.get('access_token') else 'no'}"
    )

    ws_url = await _build_signalr_url(params)
    if not ws_url:
        raise RuntimeError("Could not build SignalR WebSocket URL")

    print(f"[signalr-backend] WS URL: {redact_text(ws_url)[:120]}...")

    result = await _signalr_chat_completion(prompt, ws_url, timeout, image_paths=image_paths, params=params)
    return result
