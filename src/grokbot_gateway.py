"""Grok Bot's own gateway, using the existing Cursor account session.

No model API key, Cursor Agent turn, or background relay is involved. Secrets
stay in memory; only the provider's HTTPS hosts may receive them.
"""

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid

BACKEND = "https://api2.cursor.sh"
MAX_BODY = 8 * 1024 * 1024


class GrokError(RuntimeError):
    def __init__(self, message, delivery=None):
        super().__init__(message)
        self.delivery = delivery


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
    if not token:
        try:
            out = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "cursor-access-token", "-w"],
                                 capture_output=True, text=True, timeout=5)
            token = out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            pass
    return token or None


def post(url, body, headers, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=dict({"Content-Type": "application/json"}, **headers))
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise GrokError("Grok Bot returned more data than this client can safely retain.")
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        # Do not echo a URL, token, account export, or upstream response body.
        label = "Sign in to Cursor again" if exc.code in (401, 403) else "Grok Bot request failed"
        raise GrokError(f"{label} (HTTP {exc.code}).", "rejected" if exc.code < 500 else "unknown") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise GrokError("Cannot reach Grok Bot. Check the connection before retrying.", "unknown") from None
    except (ValueError, TypeError):
        raise GrokError("Grok Bot returned an invalid response.", "unknown") from None


class Gateway:
    def __init__(self, token=None, request=post):
        self.token = token
        self.request = request
        self.session = None

    def account(self, method, body=None, service="GrokBotService"):
        if not self.token:
            self.token = cursor_token()
        if not self.token:
            raise GrokError("No Cursor account session. Run cursor-cli login, then grokbot status.")
        return self.request(BACKEND + "/aiserver.v1." + service + "/" + method, body or {},
                            {"Authorization": "Bearer " + self.token, "Connect-Protocol-Version": "1"})

    def connect(self):
        if self.session:
            return
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
        self.connect()
        return self.request(self.session[0] + "/api/" + method, body or {}, self.session[1])

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
        body = {"agentId": ident, "prompt": prompt, "clientNonce": nonce}
        if reply_to:
            body["replyToId"] = reply_to
        if machine:
            body["machineId"] = machine
        try:
            receipt = self.call("sendPrompt", body)
        except GrokError as exc:
            if exc.delivery != "rejected":
                raise GrokError("Delivery is unknown. Resume this session to inspect the existing turn; do not resend.", "unknown") from None
            raise
        if isinstance(receipt, dict) and receipt.get("accepted") is True:
            # Current gateway acknowledges acceptance without a message ID. The
            # transcript's clientNonce supplies the exact request anchor.
            return receipt.get("messageId")
        if not isinstance(receipt, dict) or not receipt.get("messageId"):
            raise GrokError("Delivery is unknown. Resume this session to inspect the existing turn; do not resend.", "unknown")
        return receipt["messageId"]

    def transcript(self, ident):
        data = self.call("getAgentTranscriptTail", {"id": ident, "limit": 200})
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

    def interrupt(self, ident):
        return self.call("interruptAgentRun", {"id": ident})

    def approval(self, ident, entry, accept):
        message = entry.get("message") or {}
        local = message.get("type") == "local-tool-permission"
        card = message.get("ask" if local else "approval") or {}
        if message.get("type") not in ("local-tool-permission", "auto-review-approval") or card.get("status") != "pending":
            raise GrokError("This approval is no longer pending. Refresh the conversation.")
        body = {"agentId": ident, "entryId": entry["id"], "requestId": card["requestId"],
                "resolution": ("allow-once" if accept else "deny") if local else ("approved" if accept else "denied")}
        return self.call("resolveLocalToolPermission" if local else "resolveAutoReviewApproval", body)


def entry_text(entry):
    for key in ("text", "prompt", "content", "message"):
        text = entry.get(key)
        if isinstance(text, str):
            return text
    msg = entry.get("message") or {}
    return msg.get("content", "") if isinstance(msg, dict) and isinstance(msg.get("content"), str) else ""


def request_id():
    return str(uuid.uuid4())
