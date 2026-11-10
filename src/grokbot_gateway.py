"""Grok Bot's own gateway, using the existing Cursor account session.

No model API key, Cursor Agent turn, or background relay is involved. Secrets
stay in memory; only the provider's HTTPS hosts may receive them.
"""

import http.client
import json
import os
import re
import ssl
import subprocess
import sys
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BACKEND = "https://api2.cursor.sh"
MAX_BODY = 8 * 1024 * 1024
TRANSCRIPT_TAIL = 200
# The gateway's own refusal, sent with a 5xx status. It means the request was turned
# away, not lost, so a refused prompt is known to be unsent and never needs watching.
# Since Grok Bot's November 2026 update (desktop 0.79) the sandbox gateway answers a
# sendPrompt from anything but the box itself with "A Sand box session may not make
# this request on this endpoint." Prompts now go through GrokBotService instead.
REFUSED = re.compile(r"may not make this request", re.I)


class GrokError(RuntimeError):
    def __init__(self, message, delivery=None, transient=False, status=None):
        super().__init__(message)
        self.delivery = delivery
        self.transient = transient   # a gateway failure that a read can safely retry
        self.status = status         # the HTTP status, when the server answered with one


# Read-only gateway methods: repeating one changes nothing, so a cold or
# restarting sandbox pod's 417/5xx is retried instead of ending the turn.
READS = ("listAgents", "getAgentTranscriptTail", "getAgentThread")
RETRY_DELAYS = (1, 2, 4)
# The gateway's answer while the sandbox pod sleeps (the desktop app's pod_hibernated).
HIBERNATED = 417


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def allowed_url(url, gateway=False):
    if not isinstance(url, str):
        raise GrokError("Invalid Grok Bot gateway address.")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (ValueError, TypeError):
        raise GrokError("Invalid Grok Bot gateway address.") from None
    domains = ("cursor.sh", "cursor.com", "cursorvm.com") if gateway else ("cursor.sh", "cursor.com")
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment
            or port not in (None, 443)
            or not any(host == d or host.endswith("." + d) for d in domains)):
        raise GrokError("Refusing to send account credentials to an unrecognized host.")
    return url.rstrip("/")


def cursor_token():
    token = os.environ.get("CURSOR_ACCESS_TOKEN", "").strip()
    if not token and sys.platform == "win32":
        # Cursor's signed-in desktop profile owns this token. Read the one
        # existing value without modifying the database or saving another copy.
        import sqlite3
        from contextlib import closing
        database = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "Cursor/User/globalStorage/state.vscdb"
        if database.is_file():
            try:
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
                    row = connection.execute("SELECT value FROM ItemTable WHERE key = ?", ("cursorAuth/accessToken",)).fetchone()
                    if row and isinstance(row[0], str):
                        token = row[0].strip()
            except sqlite3.Error:
                pass
    if not token and sys.platform != "win32":
        try:
            out = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "cursor-access-token", "-w"],
                                 capture_output=True, text=True, timeout=5)
            token = out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            pass
    return token or None


# Requests that change nothing when repeated. Only these are resent after a pooled
# connection turns out to have been closed by the server while it sat idle.
IDEMPOTENT = {"listAgents", "getAgentTranscriptTail", "getAgentThread", "getHostSettings", "EnsureSandBox",
              "ListSandMachines", "GetFilteredUsageEvents", "GetGrokBotSendStatus"}
# Idle HTTPS connections, shared by every thread: the interactive CLI runs each turn
# on a new thread, and a new TLS handshake costs 100 to 300 ms per host. Both Cursor
# hosts kept an idle connection open for over a minute when measured (November 2026).
KEEP_IDLE = 55          # seconds an idle connection serves a request that is safe to repeat
KEEP_IDLE_WRITE = 30    # and a write, which is never resent after a connection drops
POOL_SIZE = 4           # idle connections kept per host
_pool = {}
_pool_lock = threading.Lock()
_tls = []


def tls():
    if not _tls:
        _tls.append(ssl.create_default_context())
    return _tls[0]


def proxied(host):
    """urllib honors HTTPS_PROXY and NO_PROXY; keep doing so where one applies."""
    try:
        return bool(urllib.request.getproxies().get("https")) and not urllib.request.proxy_bypass(host)
    except Exception:
        return False


def exchange(url, data, headers, timeout):
    """POST and return (status, body) with at most MAX_BODY + 1 bytes of body.

    Redirects are never followed: a 3xx is returned like any other failure status."""
    parts = urllib.parse.urlsplit(allowed_url(url, gateway=True))
    if proxied(parts.hostname):
        try:
            with urllib.request.build_opener(NoRedirect).open(
                    urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as response:
                return response.status, response.read(MAX_BODY + 1)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(MAX_BODY + 1) if exc.fp else b""
    key = (parts.hostname, parts.port or 443)
    method = parts.path.rsplit("/", 1)[-1]
    safe = method in IDEMPOTENT
    for attempt in (0, 1):
        # A retry after a dropped connection always opens a fresh one.
        connection = checkout(key, KEEP_IDLE if safe else KEEP_IDLE_WRITE) if attempt == 0 else None
        reused = connection is not None
        if not reused:
            connection = http.client.HTTPSConnection(key[0], key[1], timeout=timeout, context=tls())
        connection.timeout = timeout
        if connection.sock:
            connection.sock.settimeout(timeout)
        try:
            connection.request("POST", parts.path or "/", body=data, headers=headers)
            response = connection.getresponse()
            body = response.read(MAX_BODY + 1)
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
            connection.close()
            # The server closed a kept-alive connection; resend only what is safe to repeat.
            if reused and attempt == 0 and safe:
                continue
            raise
        except BaseException:
            connection.close()
            raise
        if len(body) > MAX_BODY or response.will_close or not response.isclosed():
            connection.close()
        else:
            checkin(key, connection)
        return response.status, body
    raise ConnectionResetError()


def checkout(key, limit):
    """The most recently used idle connection to key, if it has been idle under limit seconds."""
    stale = []
    with _pool_lock:
        idle = _pool.get(key) or []
        found = None
        while idle and found is None:
            connection, used = idle.pop()
            if time.monotonic() - used < limit:
                found = connection
            else:
                stale.append(connection)
                stale += [c for c, _ in idle]   # the rest are older still
                idle.clear()
    for connection in stale:
        connection.close()
    return found


def checkin(key, connection):
    with _pool_lock:
        idle = _pool.setdefault(key, [])
        idle.append((connection, time.monotonic()))
        extra = idle[:-POOL_SIZE]
        del idle[:-POOL_SIZE]
    for old, _ in extra:
        old.close()


def prime(url, timeout=10):
    """Open the TLS connection to url ahead of its first request (no request is sent)."""
    try:
        parts = urllib.parse.urlsplit(allowed_url(url, gateway=True))
        if proxied(parts.hostname):
            return
        connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout, context=tls())
        connection.connect()
    except (GrokError, OSError, http.client.HTTPException):
        return
    checkin((parts.hostname, parts.port or 443), connection)


def post(url, body, headers, timeout=30):
    try:
        status, raw = exchange(url, json.dumps(body).encode("utf-8"),
                               dict({"Content-Type": "application/json"}, **headers), timeout)
    except GrokError:
        raise
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
        raise GrokError("Cannot reach Grok Bot. Check the connection before retrying.", "unknown", transient=True) from None
    if not 200 <= status < 300:
        failure(status, raw)
    if len(raw) > MAX_BODY:
        raise GrokError("Grok Bot returned more data than this client can safely retain.")
    try:
        return json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        raise GrokError("Grok Bot returned an invalid response.", "unknown") from None


# Connect's error codes, named for the user instead of echoing the server's text.
CONNECT_CODES = {"resource_exhausted": "Grok Bot usage or rate limit reached",
                 "permission_denied": "This Cursor account may not do that in Grok Bot",
                 "unauthenticated": "Sign in to Cursor again",
                 "invalid_argument": "Grok Bot rejected the request as invalid",
                 "failed_precondition": "Grok Bot is not ready for that request",
                 "not_found": "Grok Bot could not find that conversation",
                 "unimplemented": "This Grok Bot server does not support that request"}


def failure(status, raw):
    detail = refusal(raw)
    if detail:
        raise GrokError(f"Grok Bot refused the request (HTTP {status}): {detail} Nothing was sent.", "rejected", status=status)
    code = connect_code(raw)
    # Do not echo a URL, token, account export, or upstream response body.
    label = CONNECT_CODES.get(code) or ("Sign in to Cursor again" if status in (401, 403) else "Grok Bot request failed")
    raise GrokError(f"{label} (HTTP {status}" + (f", {code}" if code else "") + ").",
                    "rejected" if status < 500 and status not in (408, 417) else "unknown",
                    transient=status in (408, 417, 500, 502, 503, 504), status=status)


def error_body(raw):
    try:
        data = json.loads(raw[:4096])
    except Exception:   # no body, not JSON
        return {}
    return data if isinstance(data, dict) else {}


def connect_code(raw):
    code = error_body(raw).get("code")
    return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code) else None


def refusal(raw):
    """The gateway's explicit refusal text from an HTTP error body, or None.

    Only a short, known refusal phrase is shown; any other body is discarded so
    tokens or account data in an upstream error are never echoed.
    """
    message = error_body(raw).get("error")
    if isinstance(message, str) and REFUSED.search(message):
        return re.sub(r"[\x00-\x1f\x7f]", "", message).strip()[:200]
    return None


class Gateway:
    def __init__(self, token=None, request=post, sleep=None):
        self.token = token
        self.request = request
        self.session = None
        self.sleep = sleep or time.sleep
        self.lock = threading.RLock()   # one EnsureSandBox at a time, shared by warm() and the turn

    def warm(self):
        """Start EnsureSandBox (0.5 s warm, about 4 s while the pod wakes) in the background,
        so the first read or send does not wait for it. Any failure surfaces on that call."""
        def run():
            try:
                self.connect()
                if self.request is post:
                    prime(self.session[0])   # the turn's first gateway read skips the TLS handshake
            except Exception:
                pass
        threading.Thread(target=run, name="grokbot-warm", daemon=True).start()

    def account(self, method, body=None, service="GrokBotService", headers=None):
        if not self.token:
            self.token = cursor_token()
        if not self.token:
            raise GrokError("No Cursor account session. Run cursor-cli login, then grokbot status.")
        return self.request(BACKEND + "/aiserver.v1." + service + "/" + method, body or {},
                            dict(headers or {}, **{"Authorization": "Bearer " + self.token, "Connect-Protocol-Version": "1"}))

    def connect(self):
        if self.session:
            return
        with self.lock:
            if not self.session:
                self.open_session()

    def open_session(self):
        url = os.environ.get("GROK_BOT_GATEWAY_URL", "").strip()
        token = os.environ.get("GROK_BOT_GATEWAY_TOKEN", "").strip()
        if bool(url) != bool(token):
            raise GrokError("Set both GROK_BOT_GATEWAY_URL and GROK_BOT_GATEWAY_TOKEN, or neither.")
        data = {"gatewayUrl": url, "gatewayToken": token} if url else self.account("EnsureSandBox")
        if not isinstance(data, dict) or not data.get("gatewayUrl") or not isinstance(data.get("gatewayToken"), str) or not data["gatewayToken"]:
            raise GrokError("Grok Bot access is not ready. Open Grok Bot and check your account access.")
        headers = data.get("gatewayHeaders") or data.get("gateway_headers") or {}
        if not isinstance(headers, dict) or not all(isinstance(k, str) for k in headers):
            raise GrokError("Grok Bot returned invalid gateway headers.")
        network = data.get("anyrunNetworkToken") or data.get("networkToken")
        if network and not headers:
            headers = {"x-anyrun-network-token": network}
        if os.environ.get("GROK_BOT_GATEWAY_HEADERS"):
            try:
                extra = json.loads(os.environ["GROK_BOT_GATEWAY_HEADERS"])
                if not isinstance(extra, dict) or not all(isinstance(k, str) for k in extra):
                    raise ValueError()
                headers = dict(headers, **extra)
            except (ValueError, TypeError):
                raise GrokError("GROK_BOT_GATEWAY_HEADERS must be a JSON object.") from None
        # Explicit auth cannot be replaced by an additional header.
        headers = {k: str(v) for k, v in headers.items() if k.lower() not in ("authorization", "host")}
        if any("\r" in k + v or "\n" in k + v for k, v in headers.items()):
            raise GrokError("Grok Bot returned invalid gateway headers.")
        self.session = (allowed_url(data["gatewayUrl"], gateway=True),
                        dict(headers, Authorization="Bearer " + data["gatewayToken"]))

    def call(self, method, body=None):
        woke = False
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                self.connect()
                return self.request(self.session[0] + "/api/" + method, body or {}, self.session[1])
            except GrokError as exc:
                if exc.status == HIBERNATED and not woke:
                    # The pod went to sleep (after about 4 idle minutes) and turned the request
                    # away unprocessed, so even a write may be asked again once, as the desktop
                    # app does. EnsureSandBox wakes it; asking at once skips the read waits.
                    woke, self.session = True, None
                    continue
                # Other writes (createAgent, host settings) are never repeated here:
                # their callers decide from the delivery state instead.
                if method not in READS or not exc.transient or attempt == len(RETRY_DELAYS):
                    raise
            self.sleep(RETRY_DELAYS[attempt])
            if attempt >= 1:
                self.session = None   # the pod may have moved; ask EnsureSandBox for its address again

    def agents(self):
        data = self.call("listAgents")
        rows = data if isinstance(data, list) else data.get("agents", []) if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise GrokError("Invalid Grok Bot roster.")
        return [row for row in rows if isinstance(row, dict) and row.get("id")]

    def agent(self, ident):
        return next((row for row in self.agents() if row["id"] == ident), None)

    def create(self, name, description):
        data = self.call("createAgent", {"name": name, "description": description,
                                         "title": "", "avatarShape": "gem", "avatarColor": "black", "origin": "user"})
        row = data.get("agent", data) if isinstance(data, dict) else None
        if not isinstance(row, dict) or not row.get("id"):
            raise GrokError("Grok Bot did not return the created conversation. Check the app before creating another.", "unknown")
        return row

    def send(self, ident, prompt, nonce, reply_to=None, machine=None):
        """Submit a prompt the way Grok Bot's desktop app does since 0.79: through the
        account's GrokBotService, not the sandbox gateway, which now refuses it.

        The nonce is the message ID, so the service answers a repeat as DUPLICATE and a
        prompt can never run twice. When the receipt is lost, the service's own send
        status decides: a prompt it never saw is submitted once more under the same ID
        (as the desktop app does), and anything else is watched, never replayed.
        Returns None: the transcript entry carrying the nonce anchors the request.
        """
        first = time.time()
        body = {"agentId": ident, "messageId": nonce, "text": prompt, "sentAtMs": str(int(first * 1000)),
                "isFork": False, "source": SURFACE_DESKTOP, "initiator": INITIATOR_USER, "sendSource": SEND_CHAT}
        if reply_to:
            body["replyToId"] = reply_to
        if machine:
            body["machineId"] = machine
        zone = timezone_name()
        for attempt in (0, 1):
            try:
                receipt = self.account("SendGrokBotUserMessage", body, headers={"x-cursor-timezone": zone} if zone else None)
            except GrokError as exc:
                if exc.delivery == "rejected":
                    raise
                status = self.send_status(ident, nonce, first)
                if status == "ACCEPTED":
                    return None
                if status == "REJECTED":
                    raise GrokError("Grok Bot rejected this request, so nothing ran.", "rejected") from None
                if status == "NOT_FOUND" and attempt == 0:
                    body["firstSentAgeMs"] = str(max(0, int((time.time() - first) * 1000)))
                    continue
                raise GrokError("Delivery is unknown. Resume this session to inspect the existing turn; do not resend.",
                                "unknown", transient=exc.transient) from None
            return delivered(receipt)

    def send_status(self, ident, nonce, first):
        """GrokBotService's record of one message ID: NOT_FOUND, ACCEPTED, REJECTED, PENDING,
        UNKNOWN_DURABILITY, or None when the lookup itself fails."""
        try:
            data = self.account("GetGrokBotSendStatus", {"agentId": ident, "messageId": nonce,
                                                         "firstSentAgeMs": str(max(0, int((time.time() - first) * 1000)))})
        except GrokError:
            return None
        return enum(data.get("status") if isinstance(data, dict) else None, "GROK_BOT_SEND_STATUS_", SEND_STATUS)

    def transcript(self, ident):
        data = self.call("getAgentTranscriptTail", {"id": ident, "limit": TRANSCRIPT_TAIL})
        rows = data.get("entries", []) if isinstance(data, dict) else []
        if not isinstance(rows, list) or not all(isinstance(e, dict) for e in rows):
            raise GrokError("Invalid Grok Bot transcript.")
        return rows

    def thread(self, ident, root):
        return self.call("getAgentThread", {"id": ident, "rootId": root})

    def machines(self):
        data = self.account("ListSandMachines", service="DashboardService")
        rows = data.get("machines", []) if isinstance(data, dict) else None
        if not isinstance(rows, list) or not all(isinstance(m, dict) and m.get("machineId") for m in rows):
            raise GrokError("Invalid Grok Bot machine roster.")
        return rows

    def usage_events(self, conversation, since_ms, pages=5):
        """Cursor's usage records for one conversation since ``since_ms``, oldest first.

        Each record names the model that served one inference call (Grok Bot's
        router reports its own label, such as grok-bot-default) with its tokens.
        """
        rows, end = [], int(time.time() * 1000) + 60_000
        for page in range(1, pages + 1):
            data = self.account("GetFilteredUsageEvents", {"startDate": str(int(since_ms)), "endDate": str(end),
                                                           "page": page, "pageSize": 100}, service="DashboardService")
            batch = data.get("usageEventsDisplay", []) if isinstance(data, dict) else []
            batch = [row for row in batch if isinstance(row, dict)] if isinstance(batch, list) else []
            rows += [row for row in batch if row.get("conversationId") == conversation and row.get("model")]
            if len(batch) < 100:
                break
        return sorted(rows, key=lambda row: int(row.get("timestamp") or 0))

    def interrupt(self, ident):
        return self.account("InterruptGrokBotAgentRun", {"agentId": ident, "reason": "user_interrupt"})

    def approval(self, ident, entry, accept):
        message = entry.get("message") or {}
        local = message.get("type") == "local-tool-permission"
        card = message.get("ask" if local else "approval") or {}
        if message.get("type") not in ("local-tool-permission", "auto-review-approval") or card.get("status") != "pending":
            raise GrokError("This approval is no longer pending. Refresh the conversation.")
        body = {"agentId": ident, "requestId": card["requestId"], "resolution": 1 if accept else 2}
        if local:
            body["entryId"] = entry["id"]
        else:
            body["approvalPlatform"] = "desktop"
        receipt = self.account("ResolveGrokBotLocalToolPermission" if local else "ResolveGrokBotAutoReviewApproval", body)
        if isinstance(receipt, dict) and receipt.get("refusal") is not None:
            raise GrokError("Grok Bot refused this approval. Refresh the conversation before trying again.", "rejected")
        return receipt


# aiserver.v1 GrokBotService enums, indexed by number. Connect JSON answers with the
# full value name (GROK_BOT_SEND_STATUS_NOT_FOUND) and omits the zero value.
SURFACE_DESKTOP, INITIATOR_USER, SEND_CHAT = 1, 1, 1
DELIVERY = ("UNSPECIFIED", "ACCEPTED_BOX", "ACCEPTED_TEMPORAL", "DUPLICATE", "REFUSED")
SEND_STATUS = ("UNSPECIFIED", "NOT_FOUND", "ACCEPTED", "REJECTED", "PENDING", "UNKNOWN_DURABILITY")


def enum(value, prefix, names):
    """A Connect JSON enum value (its full name or its number) as its short name."""
    if value is None:
        return names[0]
    if isinstance(value, str) and value.startswith(prefix) and value[len(prefix):] in names:
        return value[len(prefix):]
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(names):
        return names[value]
    return None


def delivered(receipt):
    """Accept a SendGrokBotUserMessage receipt, or raise with what is known about delivery."""
    delivery = enum(receipt.get("delivery"), "GROK_BOT_USER_MESSAGE_DELIVERY_", DELIVERY) if isinstance(receipt, dict) else None
    if delivery in ("ACCEPTED_BOX", "ACCEPTED_TEMPORAL", "DUPLICATE"):
        return None
    if delivery == "REFUSED":
        refused = receipt.get("refusal") if isinstance(receipt.get("refusal"), dict) else {}
        # The service's refusal is written for people (the desktop app shows it as is).
        detail = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(refused.get(key) or "")).strip()[:200]
                          for key in ("message", "failureCode") if refused.get(key))
        raise GrokError("Grok Bot refused the request" + (": " + detail if detail else "") + ". Nothing was sent.", "rejected")
    raise GrokError("Delivery is unknown. Resume this session to inspect the existing turn; do not resend.", "unknown")


def timezone_name():
    """The IANA zone the desktop app reports as x-cursor-timezone, when it can be read."""
    name = os.environ.get("TZ", "").lstrip(":")
    if not name and sys.platform != "win32":
        try:
            target = os.path.realpath("/etc/localtime")
            name = target.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in target else ""
        except OSError:
            name = ""
    return name if re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){1,2}", name) else None


def entry_text(entry):
    for key in ("text", "prompt", "content", "message"):
        text = entry.get(key)
        if isinstance(text, str):
            return text
    msg = entry.get("message") or {}
    return msg.get("content", "") if isinstance(msg, dict) and isinstance(msg.get("content"), str) else ""


def request_id():
    return str(uuid.uuid4())
