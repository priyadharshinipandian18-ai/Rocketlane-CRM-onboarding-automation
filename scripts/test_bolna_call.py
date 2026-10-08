"""Smoke-test the Bolna agent on its own — no email, no graph, no database.

Places one call with the same code the workflow uses, waits for Bolna to
finish post-call processing, then prints the transcript, the raw
extracted_data, and what the workflow would decide from it.

    python scripts/test_bolna_call.py +919876543210
    python scripts/test_bolna_call.py +919876543210 --customer "Acme Corp" --ae "Priya Dharshini"

Use your own (verified) number and answer the call. Note: the webhook will
also fire for this call, but the app ignores it — this call isn't part of
any onboarding.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.config.settings import get_settings
from src.integrations.voice_ai_client import BolnaVoiceClient, decide_outcome, is_terminal, parse_extraction

POLL_SECONDS = 5
TIMEOUT_SECONDS = 600


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("phone", help="Number to call, E.164 format, e.g. +919876543210")
    parser.add_argument("--customer", default="Acme Corp")
    parser.add_argument("--ae", default="Priya")
    args = parser.parse_args()

    settings = get_settings()
    missing = [n for n in ("bolna_api_key", "bolna_agent_id") if not getattr(settings, n)]
    if missing:
        print(f"Missing in .env: {', '.join(m.upper() for m in missing)}")
        return 1

    client = BolnaVoiceClient()
    execution_id = await client.send_call(
        phone_number=args.phone, ae_name=args.ae, customer_name=args.customer,
        metadata={"onboarding_id": "smoke-test", "attempt_no": 1},
    )
    print(f"Call placed — execution_id={execution_id}. Answer your phone.\n")

    last_status, started = None, time.monotonic()
    execution: dict = {}
    while time.monotonic() - started < TIMEOUT_SECONDS:
        execution = await client.get_execution(execution_id)
        status = (execution.get("status") or "").lower()
        if status != last_status:
            print(f"  [{int(time.monotonic() - started):>3}s] status: {status}")
            last_status = status
        if is_terminal(status):
            break
        await asyncio.sleep(POLL_SECONDS)
    else:
        print("Timed out waiting for the call to finish.")
        return 1

    telephony = execution.get("telephony_data") or {}
    print("\n── Transcript ──────────────────────────────")
    print(execution.get("transcript") or "(none)")
    print("\n── Raw extracted_data ──────────────────────")
    print(json.dumps(execution.get("extracted_data"), indent=2))

    extraction = parse_extraction(execution.get("extracted_data"))
    result = decide_outcome(
        execution.get("status"), extraction["plan_tier"], extraction["readback_confirmed"],
        extraction["confidence"], settings.bolna_min_confidence, telephony.get("hangup_reason"),
    )
    print("\n── What the workflow would do ──────────────")
    print(f"  parsed: plan_tier={extraction['plan_tier']} readback_confirmed={extraction['readback_confirmed']} "
          f"confidence={extraction['confidence']}")
    print(f"  outcome: {result}")
    if result["outcome"] == "confirmed":
        print("  -> tier accepted; the Rocketlane project would be created.")
    elif extraction["plan_tier"] == "unknown" and execution.get("extracted_data"):
        print("  -> plan_tier came back unknown. If you DID confirm a plan on the call, check the extraction")
        print("     names/options in Bolna match docs/bolna_agent_setup.md (plan_tier, readback_confirmed).")
    else:
        print("  -> not confirmed; the workflow would retry the call later.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
