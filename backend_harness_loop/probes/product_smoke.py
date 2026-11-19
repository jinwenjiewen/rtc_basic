"""Small authenticated HTTP smoke against an already identified product server.

Uses existing local account credentials without printing them, owns only its
new sessions, preserves every report, and records no Cookie, user ID or text.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit

from dotenv import dotenv_values
import httpx

from .business_smoke import AMOUNT, Ledger, PROMPTS


async def run(args):
    parsed = urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path not in {"", "/"}:
        raise ValueError("Product smoke requires an explicit loopback URL")
    credentials = {**dotenv_values(args.auth_env_file), **os.environ}
    report = {"schema_version": 1, "milestone": "M4", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "evidence_type": "real_postgresql_login_real_model_real_knowledge_http", "scope": "basic_product_text_and_auth",
        "first_output_deadline_ms": 9000, "budgets_increased": False, "automatic_retries": 0,
        "real_rtc_tested": False, "production_slo_passed": False, "checks": {}, "rounds": [], "cleanup": []}
    ledger = Ledger(args.database)
    owned = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=35, trust_env=False, follow_redirects=False) as client:
        logged_in = False
        try:
            health = await client.get("/healthz")
            if health.status_code != 200 or health.json().get("service") != "backend_harness_loop.product":
                raise ValueError("Product server identity mismatch")
            report["checks"]["product_identity"] = True
            report["checks"]["anonymous_rejected"] = (await client.get("/api/agent/models")).status_code == 401
            login = await client.post("/api/auth/login", json={"username": credentials.get("ADMIN_USERNAME", ""),
                "password": credentials.get("ADMIN_PASSWORD", ""), "remember": False}, headers={"Origin": args.base_url})
            report["checks"]["existing_account_login"] = login.status_code == 200
            if login.status_code != 200:
                return report
            logged_in = True
            client.headers.update({"X-CSRF-Token": client.cookies["rtc_csrf"], "Origin": args.base_url})
            report["checks"]["current_account"] = (await client.get("/api/auth/me")).status_code == 200
            old_refresh = client.cookies["rtc_refresh"]
            response = await client.post("/api/auth/refresh")
            report["checks"]["refresh_rotates"] = response.status_code == 200 and client.cookies["rtc_refresh"] != old_refresh
            blocked = await client.post("/api/agent/text/sessions", json={}, headers={"X-CSRF-Token": ""})
            report["checks"]["csrf_rejected"] = blocked.status_code == 403
            menu = (await client.get("/api/agent/models")).json()
            scenes = await client.post("/getScenes", json={})
            report["checks"]["formal_scene_contract"] = scenes.status_code == 200 and bool(scenes.json()["Result"]["scenes"])
            report["checks"]["react_chat_hosted"] = (await client.get("/chat")).status_code == 200
            report["rtc_available"] = menu["rtc_available"]
            for profile in menu["profiles"]:
                if not profile["configured"]:
                    continue
                response = await client.post("/api/agent/text/sessions", json={"profile_id": profile["id"]})
                if response.status_code != 200:
                    report["rounds"].append({"profile_id": profile["id"], "failure": "session_create_failed", "passed": False})
                    continue
                session = response.json()["session_id"]
                owned.append(session)
                prompts = PROMPTS[:2] if profile["id"] == menu["default_profile"] else PROMPTS[:1]
                for prompt in prompts:
                    round_id = uuid.uuid4().hex
                    started, first_ms, turn_id, reply, terminal = time.monotonic(), None, None, "", {}
                    async with client.stream("POST", f"/api/agent/text/sessions/{session}/turns", json={"round_id": round_id, "text": prompt}) as stream:
                        if stream.status_code != 200:
                            report["rounds"].append({"profile_id": profile["id"], "http_status": stream.status_code, "passed": False})
                            continue
                        event = ""
                        async for line in stream.aiter_lines():
                            if line.startswith("event:"):
                                event = line[6:].strip()
                            elif line.startswith("data:"):
                                payload = json.loads(line[5:])
                                if event == "turn":
                                    turn_id = payload["turn_id"]
                                elif event == "delta":
                                    if first_ms is None and payload["text"].strip():
                                        first_ms = round((time.monotonic() - started) * 1000, 3)
                                    reply += payload["text"]
                                elif event == "terminal":
                                    terminal = payload
                    observations, _materials = ledger.observations(turn_id, profile["id"])
                    first = observations["model_first_output"]
                    checks = {"completed": terminal.get("state") == "generation_completed" and terminal.get("error_code") is None,
                        "nonblank_reply": bool(reply.strip()), "profile_pinned": observations["profile_pinned"],
                        "native_chain_closed": observations["native_chain_closed"],
                        "default_deadline_in_effect": first is not None and first["deadline_ms"] == 9000,
                        "real_first_within_default": first is not None and first["elapsed_ms"] <= 9000,
                        "no_numeric_price": AMOUNT.search(reply) is None,
                        "no_invented_link": "http://" not in reply and "https://" not in reply}
                    if prompt == PROMPTS[0]:
                        checks["real_knowledge_used"] = any(t["name"] == "knowledge_search" and t["status"] == "success" for t in observations["tools"])
                    report["rounds"].append({"profile_id": profile["id"], "purpose": "catalog" if prompt == PROMPTS[0] else "no_quote_intent",
                        "state": terminal.get("state"), "error_code": terminal.get("error_code"),
                        "reply_sha256": hashlib.sha256(reply.encode()).hexdigest(), "reply_characters": len(reply),
                        "http_first_text_ms": first_ms, "http_turn_ms": round((time.monotonic() - started) * 1000, 3),
                        "observations": observations, "checks": checks, "passed": all(checks.values())})
                stopped = await client.post(f"/api/agent/text/sessions/{session}/stop", json={})
                report["cleanup"].append({"stopped": stopped.status_code == 200 and stopped.json().get("stopped") is True})
        except Exception as exc:
            report["failure_type"] = type(exc).__name__
        finally:
            for session in owned:
                try:
                    await client.post(f"/api/agent/text/sessions/{session}/stop", json={})
                except httpx.HTTPError:
                    pass
            if logged_in:
                response = await client.post("/api/auth/logout")
                report["checks"]["logout"] = response.status_code == 204
                report["checks"]["logout_revoked"] = (await client.get("/api/auth/me")).status_code == 401
    report["passed"] = (all(report["checks"].values()) and bool(report["rounds"])
        and all(row["passed"] for row in report["rounds"]) and all(row["stopped"] for row in report["cleanup"]) and "failure_type" not in report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:3101")
    parser.add_argument("--auth-env-file", type=Path, default=Path("backend_harness_loop/.local/product-auth.env"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Existing evidence must be preserved; choose a new output path")
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report.get("passed", False), "rounds": len(report["rounds"]), "failure_type": report.get("failure_type")}))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
