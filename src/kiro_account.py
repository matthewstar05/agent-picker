"""Read Kiro account data without launching the CLI or refreshing credentials."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

REGIONS = {"us-east-1", "eu-central-1", "us-gov-east-1", "us-gov-west-1"}
MAX_BODY = 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def credentials(path=None):
    path = path or Path.home() / "Library/Application Support/kiro-cli/data.sqlite3"
    try:
        with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            rows = db.execute("SELECT value FROM auth_kv WHERE key LIKE 'kirocli:%:token'").fetchall()
        valid = []
        for (raw,) in rows:
            token = json.loads(raw)
            expires = datetime.fromisoformat(token["expires_at"].replace("Z", "+00:00"))
            arn = token.get("profile_arn", "")
            parts = arn.split(":")
            if (expires.tzinfo is None or expires <= datetime.now(timezone.utc)
                    or not isinstance(token.get("access_token"), str) or not token["access_token"]
                    or len(parts) < 6 or parts[0] != "arn" or parts[2] != "codewhisperer"
                    or parts[3] not in REGIONS):
                continue
            valid.append((token["access_token"], arn, parts[3]))
        return valid[0] if len(valid) == 1 else None
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        return None


def account(method, query=None):
    if method not in ("Get-Usage-Limits", "List-Available-Models"):
        raise ValueError("Unsupported Kiro account read")
    saved = credentials()
    if not saved:
        return None
    token, arn, region = saved
    params = dict(query or {}, origin="KIRO_CLI", profileArn=arn)
    url = "https://management." + region + ".kiro.dev/" + method + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308, 401, 403):
            return None  # login remains an explicit action in the native CLI
        raise


def usage():
    data = account("Get-Usage-Limits")
    if not data:
        return None
    for row in data.get("usageBreakdownList") or []:
        if not isinstance(row, dict) or row.get("resourceType") != "CREDIT":
            continue
        used = row.get("currentUsageWithPrecision")
        limit = row.get("usageLimitWithPrecision")
        used = row.get("currentUsage") if used is None else used
        limit = row.get("usageLimit") if limit is None else limit
        reset = row.get("nextDateReset") or data.get("nextDateReset")
        if (not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
                    for n in (used, limit, reset)) or used < 0 or limit <= 0 or reset <= 0):
            continue
        return [{"label": "mo", "pct": 100 * used / limit, "resets": reset, "used": used, "limit": limit}]
    return None


def models():
    rows, seen, query = [], set(), {}
    for _ in range(5):
        data = account("List-Available-Models", query)
        if not data or not isinstance(data.get("models"), list):
            return []
        for model in data["models"]:
            if not isinstance(model, dict) or not isinstance(model.get("modelId"), str):
                continue
            ident = model["modelId"]
            if ident and ident not in seen:
                seen.add(ident)
                rows.append({"id": ident, "name": model.get("modelName") or ident,
                             "efforts": ["low", "medium", "high", "xhigh", "max"] if ident != "auto" else []})
        token = data.get("nextToken")
        if not token:
            return rows
        if not isinstance(token, str) or token == query.get("nextToken"):
            return []
        query = {"nextToken": token}
    return []
