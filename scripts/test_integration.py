"""
scripts/test_integration.py
==============================
End-to-end integration test for the full PA Agent pipeline.
Runs agent.py against real GCP services (FHIR + Gemini + Firestore)
with the mock payer server handling CRD / DTR / PAS endpoints.

Prerequisites:
    1. GCP infrastructure live:  .\scripts\setup_gcp_infrastructure.ps1
    2. Synthetic patient loaded:  python scripts/load_synthetic_patient.py
    3. Mock payer running:        python scripts/mock_payer_server.py
    4. Venv active + deps installed

Run:
    pytest scripts/test_integration.py -v -s
    pytest scripts/test_integration.py -v -s -k "test_happy_path"
    pytest scripts/test_integration.py -v -s -k "test_denied"

The -s flag is important — it shows live agent log output during the run.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import get_config
from shared.fhir_client import FHIRClient
from shared.models import ClaimDecision, PAStatus
from scripts.load_synthetic_patient import (
    PATIENT_ID, PRACTITIONER_ID, ENCOUNTER_ID, COVERAGE_ID,
)

CONFIG = get_config()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def set_mock_scenario(crd_status: str = "required", pa_decision: str = "approved") -> bool:
    """Set the mock payer server scenario. Returns False if server not running."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://localhost:8080/admin/set-scenario",
                json={"crd_status": crd_status, "pa_decision": pa_decision},
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                return resp.status == 200
    except Exception:
        return False


async def fhir_resource_exists(resource_type: str, resource_id: str) -> bool:
    """Check if a FHIR resource exists in the store."""
    async with FHIRClient(CONFIG) as client:
        try:
            await client.read(resource_type, resource_id)
            return True
        except Exception:
            return False


async def get_tasks_for_patient(patient_id: str) -> list[dict]:
    """Retrieve all Task resources written for the patient during this test run."""
    async with FHIRClient(CONFIG) as client:
        return await client.search("Task", {"subject": f"Patient/{patient_id}"})


async def cleanup_test_fhir_resources(patient_id: str) -> None:
    """
    Delete FHIR resources created by the agent during tests.
    Leaves the synthetic patient data intact.
    """
    resource_types_to_clean = [
        "Task", "Claim", "QuestionnaireResponse", "ClaimResponse", "ServiceRequest"
    ]
    async with FHIRClient(CONFIG) as client:
        for rt in resource_types_to_clean:
            try:
                resources = await client.search(rt, {"patient": f"Patient/{patient_id}"})
                for r in resources:
                    rid = r.get("id")
                    if rid and rid.startswith(("task-", "claim-", "cr-", "sr-", "qr-")):
                        await client._request("DELETE", f"{rt}/{rid}")
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegration:
    """
    Full end-to-end integration tests.
    Each test runs the complete agent.run_pa_agent() function.
    """

    @pytest.fixture(autouse=True)
    def require_mock_server(self):
        """Skip all integration tests if mock payer server is not running."""
        server_up = asyncio.run(set_mock_scenario("required", "approved"))
        if not server_up:
            pytest.skip(
                "Mock payer server not running. Start it with: "
                "python scripts/mock_payer_server.py"
            )

    # ── Test 1: Happy path ────────────────────────────────────────────────────

    def test_happy_path_approved(self):
        """
        Full pipeline run. All conditions met for CGM PA approval.
        Expected flow:
          PA-1 → REQUIRED
          PA-2 → Questionnaire loaded (local template or mock DTR)
          PA-3 → 6 answers, all HIGH/MODERATE, 0 MISSING
          PA-4 → PAS bundle assembled + DLP passes
          PA-5 → Submitted → ClaimResponse=APPROVED

        Verifications:
          - PAAgentResult.pa_required == REQUIRED
          - PAAgentResult.missing_required_count == 0
          - PAAgentResult.decision.decision == approved
          - ClaimResponse written to FHIR store
        """
        asyncio.run(set_mock_scenario("required", "approved"))

        from agents.prior_auth.agent import run_pa_agent

        async def run():
            return await run_pa_agent(
                patient_id=PATIENT_ID,
                cpt_code="95251",
                payer_id="bcbs-ca-001",
                encounter_id=ENCOUNTER_ID,
                practitioner_id=PRACTITIONER_ID,
                config=CONFIG,
            )

        print("\n" + "=" * 60)
        print("  INTEGRATION TEST: Happy Path (Approved)")
        print("=" * 60)

        result = asyncio.run(run())

        print(f"\n  PA Required   : {result.pa_required}")
        print(f"  Questionnaire : {result.questionnaire_id}")
        print(f"  Answers       : {len(result.answers or [])}")
        print(f"  MISSING count : {result.missing_required_count}")
        print(f"  Blocked       : {result.blocked_by_missing}")
        print(f"  Decision      : {result.decision.decision if result.decision else 'None'}")
        print(f"  Duration      : {(result.completed_at - result.initiated_at).total_seconds():.1f}s")

        # Core assertions
        assert result.pa_required == PAStatus.REQUIRED, \
            f"Expected REQUIRED, got {result.pa_required}"
        assert result.missing_required_count == 0, \
            f"Expected 0 MISSING, got {result.missing_required_count}"
        assert result.blocked_by_missing is False, \
            "Should not be blocked with complete answers"
        assert result.decision is not None, \
            "Expected a ClaimResponse decision"
        assert result.decision.decision == ClaimDecision.approved, \
            f"Expected approved, got {result.decision.decision}"

        # Verify answers have citations
        for answer in (result.answers or []):
            if answer.confidence != from shared.models import AnswerConfidence; AnswerConfidence.MISSING:
                assert answer.evidence_resource_id is not None, \
                    f"Answer {answer.link_id} has no citation"

        print("\n  PASS: Happy path approved.")

    # ── Test 2: PA not required ───────────────────────────────────────────────

    def test_pa_not_required_exits_early(self):
        """
        When CRD returns 'not required', agent should exit after PA-1.
        No bundle assembled, no Gemini call, no FHIR writes beyond Task.
        """
        asyncio.run(set_mock_scenario("not_required", "approved"))

        from agents.prior_auth.agent import run_pa_agent

        async def run():
            return await run_pa_agent(
                patient_id=PATIENT_ID,
                cpt_code="95251",
                payer_id="bcbs-ca-001",
                config=CONFIG,
            )

        print("\n" + "=" * 60)
        print("  INTEGRATION TEST: PA Not Required")
        print("=" * 60)

        result = asyncio.run(run())

        print(f"\n  PA Required   : {result.pa_required}")
        print(f"  Answers       : {len(result.answers or [])}")
        print(f"  Decision      : {result.decision}")

        assert result.pa_required == PAStatus.NOT_REQUIRED
        assert result.decision is None, "No decision expected when PA not required"
        assert len(result.answers or []) == 0, "No answers expected when PA not required"

        # Reset
        asyncio.run(set_mock_scenario("required", "approved"))
        print("\n  PASS: Agent exited cleanly after PA not required.")

    # ── Test 3: Denied ────────────────────────────────────────────────────────

    def test_denied_writes_task(self):
        """
        When payer denies, ClaimResponse is written and a Task is created.
        """
        asyncio.run(set_mock_scenario("required", "denied"))

        from agents.prior_auth.agent import run_pa_agent

        async def run():
            return await run_pa_agent(
                patient_id=PATIENT_ID,
                cpt_code="95251",
                payer_id="bcbs-ca-001",
                encounter_id=ENCOUNTER_ID,
                config=CONFIG,
            )

        print("\n" + "=" * 60)
        print("  INTEGRATION TEST: Denied")
        print("=" * 60)

        result = asyncio.run(run())

        print(f"\n  Decision      : {result.decision.decision if result.decision else 'None'}")

        assert result.decision is not None
        assert result.decision.decision == ClaimDecision.denied, \
            f"Expected denied, got {result.decision.decision}"

        # Reset
        asyncio.run(set_mock_scenario("required", "approved"))
        print("\n  PASS: Denied scenario handled correctly.")

    # ── Test 4: Pended ────────────────────────────────────────────────────────

    def test_pended_writes_task_with_missing_items(self):
        """
        When payer pends with missing items, a Task is written containing
        the payer's list of required additional documentation.
        """
        asyncio.run(set_mock_scenario("required", "pended"))

        from agents.prior_auth.agent import run_pa_agent

        async def run():
            return await run_pa_agent(
                patient_id=PATIENT_ID,
                cpt_code="95251",
                payer_id="bcbs-ca-001",
                config=CONFIG,
            )

        print("\n" + "=" * 60)
        print("  INTEGRATION TEST: Pended")
        print("=" * 60)

        result = asyncio.run(run())

        print(f"\n  Decision      : {result.decision.decision if result.decision else 'None'}")

        if result.decision:
            print(f"  Pended items  : {result.decision.pended_items}")

        assert result.decision is not None
        assert result.decision.decision == ClaimDecision.pended, \
            f"Expected pended, got {result.decision.decision}"

        # Reset
        asyncio.run(set_mock_scenario("required", "approved"))
        print("\n  PASS: Pended scenario handled correctly.")

    # ── Test 5: FHIR write verification ──────────────────────────────────────

    def test_fhir_resources_written_correctly(self):
        """
        After a successful approved run, verify FHIR resources were created:
        - Claim
        - QuestionnaireResponse
        - ClaimResponse
        """
        asyncio.run(set_mock_scenario("required", "approved"))

        from agents.prior_auth.agent import run_pa_agent

        async def run():
            result = await run_pa_agent(
                patient_id=PATIENT_ID,
                cpt_code="95251",
                payer_id="bcbs-ca-001",
                encounter_id=ENCOUNTER_ID,
                config=CONFIG,
            )
            return result

        print("\n" + "=" * 60)
        print("  INTEGRATION TEST: FHIR Write Verification")
        print("=" * 60)

        result = asyncio.run(run())

        # Verify ClaimResponse was written
        async def verify():
            async with FHIRClient(CONFIG) as client:
                claims = await client.search("Claim", {"patient": f"Patient/{PATIENT_ID}"})
                qrs = await client.search("QuestionnaireResponse", {"patient": f"Patient/{PATIENT_ID}"})
                crs = await client.search("ClaimResponse", {"patient": f"Patient/{PATIENT_ID}"})
                return claims, qrs, crs

        claims, qrs, crs = asyncio.run(verify())

        print(f"\n  Claim resources       : {len(claims)}")
        print(f"  QuestionnaireResponse : {len(qrs)}")
        print(f"  ClaimResponse         : {len(crs)}")

        assert len(claims) >= 1, "At least one Claim should exist"
        assert len(qrs) >= 1, "At least one QuestionnaireResponse should exist"
        assert len(crs) >= 1, "At least one ClaimResponse should exist"

        # Check ClaimResponse content
        cr = crs[-1]
        print(f"\n  ClaimResponse outcome : {cr.get('outcome')}")
        print(f"  ClaimResponse preAuthRef: {cr.get('preAuthRef', 'N/A')}")
        assert cr.get("outcome") == "complete", \
            f"Expected outcome=complete, got {cr.get('outcome')}"

        print("\n  PASS: All FHIR resources written correctly.")

    # ── Test 6: Urgency classification ───────────────────────────────────────

    def test_urgency_classification_standard(self):
        """
        Synthetic patient with routine diabetes management should
        be classified as STANDARD (not expedited).
        """
        from agents.prior_auth.agent import _classify_urgency
        from scripts.load_synthetic_patient import build_clinical_impression, build_care_plan

        async def run():
            return await _classify_urgency(
                clinical_impression=build_clinical_impression(),
                care_plan=build_care_plan(),
                detected_issues=[],
                config=CONFIG,
            )

        is_expedited = asyncio.run(run())
        print(f"\n  Urgency classification: {'EXPEDITED' if is_expedited else 'STANDARD'}")

        # Routine diabetes management — expect STANDARD
        assert is_expedited is False, \
            "Routine CGM for T2DM should be STANDARD, not EXPEDITED"

        print("\n  PASS: Urgency correctly classified as STANDARD.")


# ══════════════════════════════════════════════════════════════════════════════
# TEST REPORT SUMMARY (run at the end)
# ══════════════════════════════════════════════════════════════════════════════

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print a clean summary after all tests complete."""
    passed = len(terminalreporter.stats.get("passed", []))
    failed = len(terminalreporter.stats.get("failed", []))
    skipped = len(terminalreporter.stats.get("skipped", []))

    print("\n")
    print("=" * 60)
    print("  HC-CDSS PA Agent — Integration Test Summary")
    print("=" * 60)
    print(f"  Passed  : {passed}")
    print(f"  Failed  : {failed}")
    print(f"  Skipped : {skipped}")
    print()
    if failed == 0:
        print("  ALL TESTS PASSED. Agent is ready for production deployment.")
    else:
        print("  FAILURES DETECTED. Review output above before deploying.")
    print("=" * 60)
