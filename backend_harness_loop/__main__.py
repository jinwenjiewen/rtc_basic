"""Run from the repository root: python -m backend_harness_loop --help."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from .config import Settings
from .reporting import write_report


async def public_capture_health(settings: Settings, url: str) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return {"name": "public_capture", "status": "blocked", "summary": "A clean public HTTPS base URL is required", "evidence": {}}
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=settings.trust_env, follow_redirects=False) as client:
            response = await client.get(url.rstrip("/") + "/healthz", headers={"ngrok-skip-browser-warning": "m0-validation"})
        payload = response.json() if response.status_code == 200 else {}
        matched = payload.get("service") == "backend_harness_loop.m0" and payload.get("instance_id") == settings.capture_instance_id
        return {"name": "public_capture", "status": "passed" if matched else "blocked", "summary": "Capture instance verified" if matched else "Public URL does not reach this capture instance", "evidence": {"http_status": response.status_code, "instance_matched": matched}}
    except Exception as exc:
        return {"name": "public_capture", "status": "blocked", "summary": "Public capture check failed", "evidence": {"error_type": type(exc).__name__}}


async def run(args: argparse.Namespace, settings: Settings) -> int:
    results = []
    if args.command in {"offline", "all"}:
        from .protocol import run_protocol_probe
        results.append(run_protocol_probe())
    if args.command in {"model", "all"}:
        from .probes.model import run_model_probe
        results.append(await run_model_probe(settings, samples=args.samples))
    if args.command in {"rtc", "all"}:
        from .probes.rtc import run_rtc_probe
        base = args.public_url or settings.server_url
        can_start = False
        if args.start:
            health = await public_capture_health(settings, base)
            results.append(health)
            can_start = health["status"] == "passed"
        callback = None
        if can_start:
            query = urlencode({"token": settings.callback_secret, "rtc_run_id": "m0_run_" + uuid.uuid4().hex})
            callback = base.rstrip("/") + "/api/chat_callback?" + query
        results.append(await run_rtc_probe(settings, start=can_start, callback_url=callback))
    if args.command == "public-health":
        results.append(await public_capture_health(settings, args.public_url or settings.server_url))
    report = write_report(args.output, results, settings)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if any(result["status"] == "failed" for result in results) else (2 if any(result["status"] == "blocked" for result in results) else 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="RTC Harness + Loop developer backend: M0 probes and M1/M2 agent runtime")
    parser.add_argument("command", choices=["doctor", "m1-doctor", "serve", "text", "product", "offline", "model", "rtc", "all", "capture", "client", "public-health"])
    parser.add_argument("--auth-env-file", type=Path, default=Path("backend_harness_loop/.local/product-auth.env"), help="Existing PostgreSQL accounts and Cookie/JWT settings; no account migration")
    parser.add_argument("--build-dir", type=Path, default=Path("build"), help="Built formal React frontend")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--overlay-env-file", type=Path, help="Explicit local overrides; environment variables still take precedence")
    parser.add_argument("--model-env-file", type=Path, default=Path("backend_harness_loop/.env"), help="Model credentials only; separate from RTC configuration")
    parser.add_argument("--models-file", type=Path, default=Path("backend_harness_loop/model_profiles.json"), help="Provider endpoints and model profiles; no literal credentials")
    parser.add_argument("--knowledge-env-file", type=Path, help="Separate read-only knowledge endpoint, resource and key; no legacy backend import")
    parser.add_argument("--knowledge-config", type=Path, help="Knowledge adapter, endpoint and resource JSON; credentials stay in --knowledge-env-file")
    parser.add_argument("--business-policy", type=Path, help="Independent operator reply policy; pricing and actual enrollment entry")
    parser.add_argument("--database", type=Path, default=Path("backend_harness_loop/.local/runtime.sqlite3"))
    parser.add_argument("--profile", help="Default model profile for new sessions; each session stays pinned")
    parser.add_argument("--output", type=Path, default=Path("backend_harness_loop/.local/m0-report.json"))
    parser.add_argument("--samples", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--trust-env", action="store_true")
    parser.add_argument("--public-url", default="")
    parser.add_argument("--start", action="store_true", help="Create only an isolated m0 RTC task, after public instance health verification")
    parser.add_argument("--port", type=int, default=3101)
    parser.add_argument("--text-probe", action="store_true", help="On an isolated started task, send two synthetic text turns and an interrupt")
    parser.add_argument("--rtc-api-version", choices=["2024-12-01", "2025-06-01"])
    parser.add_argument("--legacy-source", type=Path, help="Read only literal ASR/TTS fields from an explicitly selected Python source file")
    parser.add_argument("--voice-config", type=Path, help="Standalone ASR/TTS JSON; product defaults to .local/voice-profile.json")
    parser.add_argument("--interrupt-probe", action="store_true", help="Send one text input, then interrupt this isolated task")
    args = parser.parse_args()
    try:
        if args.legacy_source and (args.voice_config or args.command == "product"):
            raise ValueError("Product does not load retired Python source; use --voice-config")
        environment_file = args.env_file
        if environment_file is None and args.command == "product":
            environment_file = Path("backend_harness_loop/.local/product-rtc.env")
        settings = Settings.load(environment_file, trust_env=args.trust_env, overlay_file=args.overlay_env_file)
        if args.text_probe:
            settings = replace(settings, rtc_text_probe=True)
        if args.interrupt_probe:
            settings = replace(settings, rtc_interrupt_only=True)
        if args.rtc_api_version:
            settings = replace(settings, rtc_api_version=args.rtc_api_version)
        if args.legacy_source:
            from .legacy import load_legacy_voice_profile
            settings = replace(settings, rtc_legacy_voice_config=load_legacy_voice_profile(args.legacy_source))
        voice_file = args.voice_config
        if voice_file is None and args.command == "product":
            voice_file = Path("backend_harness_loop/.local/voice-profile.json")
        if voice_file is not None:
            from .voice import load_voice_profile
            settings = replace(settings, rtc_legacy_voice_config=load_voice_profile(voice_file))
    except (ValueError, OSError, SyntaxError) as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}))
        return 2
    if args.command == "doctor":
        print(json.dumps(settings.inventory(), ensure_ascii=False, indent=2))
        return 0
    profiles = None
    if args.command in {"serve", "text", "product", "m1-doctor"}:
        from .models import ModelProfiles, ModelError, load_model_environment
        from .knowledge import KnowledgeConfig, KnowledgeError
        from .business import BusinessPolicy
        try:
            settings = replace(settings, knowledge_config=(KnowledgeConfig.load_file(args.knowledge_config, args.knowledge_env_file)
                if args.knowledge_config else KnowledgeConfig.load(args.knowledge_env_file)))
            if args.business_policy:
                settings = replace(settings, business_policy=BusinessPolicy.load(args.business_policy))
            values = load_model_environment(args.model_env_file if args.model_env_file.exists() else None)
            profiles = ModelProfiles.load(args.models_file, environ=values)
            if args.profile:
                profiles.get(args.profile)
                profiles.default_profile_id = args.profile
        except ModelError as exc:
            print(json.dumps({"status": "blocked", "code": exc.code}))
            return 2
        except KnowledgeError as exc:
            print(json.dumps({"status": "blocked", "code": exc.code}))
            return 2
        except ValueError:
            print(json.dumps({"status": "blocked", "code": "invalid_business_policy"}))
            return 2
        if args.command == "m1-doctor":
            ready = all(item["configured"] for item in profiles.public_inventory()["profiles"])
            print(json.dumps({"status": "configured" if ready else "blocked", "runtime_mode": "agent_loop",
                "knowledge": settings.inventory()["knowledge"], "business": settings.inventory()["business"], **profiles.public_inventory()}, ensure_ascii=False, indent=2))
            return 0 if ready else 2
    if args.command == "product":
        import uvicorn
        from .product_auth import AuthSettings, AuthService
        from .product import create_product_app
        try:
            auth_settings = AuthSettings.load(args.auth_env_file)
            if not auth_settings.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("Product entry requires the existing PostgreSQL account store")
            # This command binds loopback only. Public origins must be explicitly
            # configured; these two origins are for its own local React hosting.
            auth_settings = replace(auth_settings, allowed_origins=tuple(dict.fromkeys([
                *auth_settings.allowed_origins, f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}"])))
            auth = AuthService(auth_settings)
            product_database = (Path("backend_harness_loop/.local/product-runtime.sqlite3")
                if args.database == Path("backend_harness_loop/.local/runtime.sqlite3") else args.database)
            app = create_product_app(settings, profiles, product_database,
                args.output if args.output != Path("backend_harness_loop/.local/m0-report.json") else Path("backend_harness_loop/.local/product-capture.jsonl"),
                auth=auth, public_url=args.public_url or settings.server_url, build_dir=args.build_dir)
        except (ValueError, OSError):
            print(json.dumps({"status": "blocked", "code": "invalid_product_configuration"}))
            return 2
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)
        return 0
    if args.command == "text":
        import uvicorn
        from .text import create_text_app
        try:
            app = create_text_app(settings, profiles, args.database, port=args.port)
        except ValueError:
            print(json.dumps({"status": "blocked", "summary": "Text runtime configuration is invalid"}))
            return 2
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)
        return 0
    if args.command in {"capture", "client", "serve"}:
        import uvicorn
        from .capture import create_capture_app
        if not settings.callback_secret:
            print(json.dumps({"status": "blocked", "summary": "RTC_CALLBACK_SECRET is required"}))
            return 2
        if args.command in {"client", "serve"}:
            from .client import create_client_app
            required = ("volc_access_key", "volc_secret_key", "rtc_app_id", "rtc_legacy_voice_config")
            missing = [key for key in required if not getattr(settings, key)]
            if not (settings.rtc_app_key or settings.rtc_static_token):
                missing.append("rtc_app_key_or_explicit_static_token")
            if missing:
                print(json.dumps({"status": "blocked", "missing_settings": missing}))
                return 2
            try:
                if args.command == "serve":
                    from .app import create_agent_app
                    if args.output == Path("backend_harness_loop/.local/m0-report.json"):
                        args.output = Path("backend_harness_loop/.local/m1-capture.jsonl")
                    app = create_agent_app(settings, profiles, args.database, args.output, args.public_url, port=args.port)
                else:
                    # Long synthetic output is M0-only; M1 always uses the model.
                    settings = replace(settings, capture_chunk_count=8, capture_chunk_delay_seconds=1, capture_fail_first=False)
                    app = create_client_app(settings, args.output, args.public_url, port=args.port)
            except ValueError:
                print(json.dumps({"status": "blocked", "summary": "Client configuration or recovery ledger is invalid; preserve the local ledger for inspection"}))
                return 2
        else:
            app = create_capture_app(settings, args.output)
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)
        return 0
    return asyncio.run(run(args, settings))


if __name__ == "__main__":
    sys.exit(main())
