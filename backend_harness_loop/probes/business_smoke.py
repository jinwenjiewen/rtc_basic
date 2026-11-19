"""Small real-model/real-knowledge text or RTC-cloud smoke, not a deep eval.

RTC input is ExternalTextToLLM into an owned cloud task, not microphone ASR.
Only metadata leaves the local ledger. Existing services/sessions are untouched.
"""

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

import httpx

from ..config import Settings
from .knowledge import _observations
from .rtc import call_openapi
from .text import DEFAULT_PROFILES, ProbeFailure, _base_url, _cleanup, _create, _failure, _page_key, _turn


PROMPTS = (
    "我完全零基础，想学编程。请查资料后推荐一门适合入门的课程，用一句话回答。",
    "这门课多少钱，有没有优惠？我有报名意向。",
    "先别报名，我现在只咨询退款条件，购买以后不合适可以退吗？请查资料确认。",
)
PURPOSES = ("real_catalog", "no_quote_enrollment", "refund_and_withdraw_intent")
AMOUNT = re.compile(r"[￥¥$]\s*\d|\d+(?:[.,~～–—-]\d+)*\s*(?:元|块|人民币|CNY|USD|美元)|[零一二三四五六七八九十百千万两]+\s*(?:元|块)|\d+\s*折", re.I)


def smoke_checks(index, text, observations, materials):
    tools = observations["tools"]
    result = {"nonblank_reply": bool(text.strip()), "profile_pinned": observations["profile_pinned"],
        "native_chain_closed": observations["native_chain_closed"],
        "real_first_within_default": observations["model_first_output"] is not None
            and observations["model_first_output"]["elapsed_ms"] <= 9000,
        "default_deadline_in_effect": observations["model_first_output"] is not None
            and observations["model_first_output"]["deadline_ms"] == 9000,
        "model_calls_bounded": observations["model_calls"] <= 4, "native_calls_bounded": len(tools) <= 8,
        "no_fabricated_completion": not any(s in text for s in ("已为你下单", "已完成报名", "订单已提交", "已为你报名", "已经付款")),
        "no_invented_link": re.search(r"https?://|www\.", text, re.I) is None}
    if index in {0, 2}:
        result["real_knowledge_used"] = any(t["name"] == "knowledge_search" and t["status"] == "success" for t in tools)
    if index == 0:
        names = [name for material in materials for name in re.findall(r"《([^》]+)》", material)]
        result["course_from_actual_material"] = any(name in text for name in names)
    elif index == 1:
        result["no_numeric_price_or_discount"] = AMOUNT.search(text) is None
        result["enrollment_guidance"] = any(s in text for s in ("报名", "意向", "确认", "课程"))
    elif index == 2:
        result["refund_days_retained"] = "7" in text or "七" in text
        result["refund_progress_condition_retained"] = "10%" in text or "10％" in text or "百分之十" in text or "十分之一" in text
    return result


class Ledger:
    def __init__(self, database):
        self.database = database
    def observations(self, turn_id, profile):
        with closing(sqlite3.connect("file:" + self.database.resolve().as_posix() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            class Store:
                pass
            store = Store()
            store.db = db
            class Runtime:
                pass
            runtime = Runtime()
            runtime.store = store
            return _observations(runtime, turn_id, profile)
    def turns(self, session):
        with closing(sqlite3.connect("file:" + self.database.resolve().as_posix() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT turn_id,state,assistant_text,delivery_status FROM turns WHERE session_id=? ORDER BY created_at", (session,))]
    async def cloud_turn(self, session, seen):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            rows = [row for row in self.turns(session) if row["turn_id"] not in seen]
            if rows and rows[-1]["state"] in {"generation_completed", "failed", "cancelled"}:
                return rows[-1]
            await asyncio.sleep(.1)
        raise ProbeFailure("rtc_cloud_callback_not_completed")


async def run(args):
    ledger = Ledger(args.database)
    settings = (Settings.load(args.env_file, overlay_file=args.overlay_env_file)
                if args.transport == "rtc" else Settings.load(None))
    report = {"schema_version": 1, "milestone": "M3", "kind": "basic_real_business_smoke",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "transport": args.transport,
        "scope": "User requested basic smoke before M4; full prompt/deep testing deferred.",
        "first_output_deadline_ms": 9000, "pricing_policy": "defer_to_checkout", "entry_configured": False,
        "microphone_asr_tested": False, "human_audio_confirmation": False, "production_slo_claim": False,
        "automatic_retries": 0, "budgets_increased": False, "rounds": [], "cleanup": [], "interrupts": []}
    report["steps_per_profile"] = args.steps
    report["rtc_input_spacing_seconds"] = 2 if args.transport == "rtc" else 0
    async with (httpx.AsyncClient(base_url=_base_url(args.base_url), timeout=30, trust_env=False) as client,
                httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as cloud):
        health = (await client.get("/healthz")).json()
        if health.get("mode") != "agent_loop":
            raise ProbeFailure("smoke_requires_agent_service")
        client.headers["X-M0-Client-Key"] = await _page_key(client)
        for profile in args.profile or DEFAULT_PROFILES:
            session = None
            try:
                if args.transport == "text":
                    session, _ = await _create(client, profile)
                else:
                    response = await client.post("/api/m0/sessions", json={"profile_id": profile})
                    if response.status_code != 200:
                        raise ProbeFailure("rtc_smoke_session_create_failed", response.status_code)
                    created = response.json()
                    session = created["session_id"]
                    if not re.fullmatch(r"m0_session_[a-f0-9]{32}", session):
                        raise ProbeFailure("rtc_smoke_owned_session_required")
                    stored = json.loads(args.rtc_ledger.read_text("utf-8"))["sessions"]
                    entry = next(row for row in stored if row["session_id"] == session)
                    identity = {"AppId": settings.rtc_app_id, "RoomId": entry["room_id"], "TaskId": entry["task_id"]}
                    started = await client.post(f"/api/m0/sessions/{session}/start", json={})
                    if started.status_code != 200 or not started.json().get("running"):
                        raise ProbeFailure("rtc_smoke_cloud_start_failed", started.status_code)
                for index, prompt in enumerate(PROMPTS[:args.steps]):
                    if args.transport == "text":
                        result = await _turn(client, session, prompt)
                        turn_id, text, state, error = result.turn_id, result.text, result.state, result.error_code
                    else:
                        if index:
                            # Space external commands after the preceding
                            # generated reply; this is outside model admission.
                            await asyncio.sleep(2)
                        seen = {row["turn_id"] for row in ledger.turns(session)}
                        update = await call_openapi(cloud, settings, "UpdateVoiceChat", {**identity,
                            "Command": "ExternalTextToLLM", "Message": prompt, "InterruptMode": 1})
                        if not update.get("ok"):
                            raise ProbeFailure("rtc_smoke_external_input_failed")
                        row = await ledger.cloud_turn(session, seen)
                        turn_id, text, state, error = row["turn_id"], row["assistant_text"], row["state"], None
                    observations, materials = ledger.observations(turn_id, profile)
                    checks = smoke_checks(index, text, observations, materials)
                    checks["completed"] = state == "generation_completed" and error is None
                    report["rounds"].append({"profile_id": profile, "purpose": PURPOSES[index],
                        "turn_sha256": hashlib.sha256(turn_id.encode()).hexdigest(), "state": state,
                        "error_code": error, "reply_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "observations": observations, "checks": checks, "passed": all(checks.values())})
                if args.transport == "rtc":
                    update = await call_openapi(cloud, settings, "UpdateVoiceChat", {**identity, "Command": "interrupt"})
                    report["interrupts"].append({"profile_id": profile, "cloud_interrupt_acknowledged": bool(update.get("ok"))})
            except Exception as exc:
                report["rounds"].append({"profile_id": profile, "failure": _failure(exc), "passed": False})
            finally:
                if session:
                    if args.transport == "text":
                        report["cleanup"].append(await _cleanup(client, args.database, session, profile))
                    else:
                        stopped = await client.post(f"/api/m0/sessions/{session}/stop", json={})
                        report["cleanup"].append({"cloud_stopped": stopped.status_code == 200 and stopped.json().get("stopped") is True,
                            "passed": stopped.status_code == 200 and stopped.json().get("stopped") is True})
    report["passed"] = (bool(report["rounds"]) and all(row["passed"] for row in report["rounds"])
        and all(row["passed"] for row in report["cleanup"]) and all(row["cloud_interrupt_acknowledged"] for row in report["interrupts"]))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("text", "rtc"), required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:3101")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--rtc-ledger", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", action="append")
    parser.add_argument("--steps", type=int, choices=(2, 3), default=2)
    parser.add_argument("--env-file", type=Path, default=Path("rag_llm_server/.env"))
    parser.add_argument("--overlay-env-file", type=Path, default=Path("backend_harness_loop/.local/client-credentials.env"))
    args = parser.parse_args()
    if args.output.exists():
        print(json.dumps({"status": "blocked", "code": "report_already_exists"}))
        return 2
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        report = {"passed": False, "failure": _failure(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "rounds": len(report.get("rounds", [])),
                      "passed_rounds": sum(row["passed"] for row in report.get("rounds", []))}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
