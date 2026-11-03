import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import kiro_account as account
import agent_picker as picker
import model_catalog

ARN = "arn:aws:codewhisperer:us-east-1:123456789012:profile/test"
USAGE = {"nextDateReset": 1796083200, "usageBreakdownList": [
    {"resourceType": "CREDIT", "currentUsageWithPrecision": 12.5, "usageLimitWithPrecision": 1000}]}


class AccountTests(unittest.TestCase):
    def test_credentials_are_read_without_mutation_and_expiry_requires_no_login(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "account.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE auth_kv (key TEXT, value TEXT)")
                for key, hours in [("kirocli:social:token", 1), ("kirocli:expired:token", -1)]:
                    db.execute("INSERT INTO auth_kv VALUES (?,?)", (key, json.dumps({
                        "access_token": "private", "profile_arn": ARN,
                        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()})))
            before = path.read_bytes()
            self.assertEqual(account.credentials(path), ("private", ARN, "us-east-1"))
            self.assertEqual(path.read_bytes(), before)
            with sqlite3.connect(path) as db:
                db.execute("DELETE FROM auth_kv WHERE key='kirocli:social:token'")
            self.assertIsNone(account.credentials(path))
            missing = Path(folder) / "missing.sqlite3"
            self.assertIsNone(account.credentials(missing))
            self.assertFalse(missing.exists())

    def test_usage_uses_precision_and_preserves_the_existing_window_contract(self):
        with patch.object(account, "account", return_value=USAGE), patch.object(picker.subprocess, "run") as run:
            self.assertEqual(picker.usage_kiro(), [{"label": "mo", "pct": 1.25, "used": 12.5,
                                                  "limit": 1000, "resets": 1796083200}])
        run.assert_not_called()

    def test_invalid_quota_never_becomes_a_fresh_reading(self):
        for bad in (None, 0, -1, float("nan"), float("inf"), "1000", True):
            data = json.loads(json.dumps(USAGE))
            data["usageBreakdownList"][0]["usageLimitWithPrecision"] = bad
            with self.subTest(limit=bad), patch.object(account, "account", return_value=data):
                self.assertIsNone(account.usage())

    def test_account_read_sends_existing_token_only_to_the_provider_and_never_follows_redirects(self):
        response = io.BytesIO(json.dumps(USAGE).encode())
        with patch.object(account, "credentials", return_value=("private", ARN, "us-east-1")), \
                patch.object(account.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = contextlib.closing(response)
            self.assertEqual(account.account("Get-Usage-Limits"), USAGE)
            req = opener.return_value.open.call_args.args[0]
            self.assertTrue(req.full_url.startswith("https://management.us-east-1.kiro.dev/Get-Usage-Limits?"))
            self.assertIn("origin=KIRO_CLI", req.full_url)
            self.assertEqual(req.get_header("Authorization"), "Bearer private")
        self.assertIsNone(account.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_expired_or_rejected_credentials_never_start_a_login(self):
        with patch.object(account, "credentials", return_value=None), \
                patch.object(account.urllib.request, "build_opener") as opener:
            self.assertIsNone(account.account("Get-Usage-Limits"))
        opener.assert_not_called()
        for code in (302, 401, 403):
            with patch.object(account, "credentials", return_value=("private", ARN, "us-east-1")), \
                    patch.object(account.urllib.request.OpenerDirector, "open", side_effect=
                                 urllib.error.HTTPError("https://management.us-east-1.kiro.dev", code, "", {}, None)):
                self.assertIsNone(account.account("Get-Usage-Limits"))

    def test_model_refresh_uses_the_same_account_without_a_cli_process(self):
        with patch.object(account, "account", side_effect=[
                {"models": [{"modelId": "auto", "modelName": "Auto"}], "nextToken": "page2"},
                {"models": [{"modelId": "opus", "modelName": "Opus"}]}]) as read, \
                patch.object(model_catalog, "resolve_argv", return_value=["/vendor/kiro-cli"]), \
                patch.object(model_catalog.subprocess, "run") as run:
            rows = model_catalog.fetch({"name": "Kiro"})
        self.assertEqual([r["id"] for r in rows], ["auto", "opus"])
        self.assertEqual(rows[0]["efforts"], [])
        self.assertEqual(read.call_args.args, ("List-Available-Models", {"nextToken": "page2"}))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
