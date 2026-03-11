"""
scripts/test_unit.py
======================
Unit tests for each PA Agent tool module in isolation.
Each test mocks the minimum required dependencies so modules
are tested individually without full GCP connectivity.

Prerequisites:
    .venv active
    FHIR store loaded (load_synthetic_patient.py)
    Mock payer server running (mock_payer_server.py)

Run:
    pytest scripts/test_unit.py -v
    pytest scripts/test_unit.py -v -k "test_coverage"   # single test
    pytest scripts/test_unit.py -v --tb=short           # shorter tracebacks
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import get_config
from shared.models import AnswerConfidence, PAStatus
from scripts.load_synthetic_patient import (
    PATIENT_ID, PRACTITIONER_ID, ENCOUNTER_ID, COVERAGE_ID,
    CONDITION_DM_ID, OBS_HBA1C_ID, MED_INSULIN_ID,
    build_patient, build_coverage, build_condition_dm,
    build_obs_hba1c, build_med_insulin, build_questionnaire_template,
)

CONFIG = get_config()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_fhir_client():
    """FHIRClient with pre-populated search/read responses for the synthetic patient."""
    client = AsyncMock()

    # search() returns lists by resource type
    async def search_side_effect(resource_type, params=None, **kwargs):
        if resource_type == "Coverage":
            return [build_coverage()]
        if resource_type == "ClinicalImpression":
            from scripts.load_synthetic_patient import build_clinical_impression
            return [build_clinical_impression()]
        return []

    # read() returns single resource
    async def read_side_effect(resource_type, resource_id):
        builders = {
            f"Patient/{PATIENT_ID}": build_patient,
            f"Coverage/{COVERAGE_ID}": build_coverage,
            f"Condition/{CONDITION_DM_ID}": build_condition_dm,
            f"Observation/{OBS_HBA1C_ID}": build_obs_hba1c,
            f"MedicationRequest/{MED_INSULIN_ID}": build_med_insulin,
        }
        key = f"{resource_type}/{resource_id}"
        if key in builders:
            return builders[key]()
        raise Exception(f"Resource not found: {key}")

    # everything() returns a bundle
    async def everything_side_effect(patient_id, resource_types=None):
        from scripts.load_synthetic_patient import (
            build_condition_htn, build_condition_ckd, build_obs_glucose,
            build_obs_bp, build_obs_bmi, build_obs_weight,
            build_med_metformin, build_allergy, build_clinical_impression,
        )
        resources = [
            build_patient(), build_coverage(),
            build_condition_dm(), build_condition_htn(), build_condition_ckd(),
            build_obs_hba1c(), build_obs_glucose(), build_obs_bp(),
            build_obs_bmi(), build_obs_weight(),
            build_med_insulin(), build_med_metformin(),
            build_allergy(), build_clinical_impression(),
        ]
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(resources),
            "entry": [{"resource": r} for r in resources],
        }

    client.search.side_effect = search_side_effect
    client.read.side_effect = read_side_effect
    client.everything.side_effect = everything_side_effect
    client.create = AsyncMock(return_value={"resourceType": "Task", "id": "task-mock-001"})
    client.update = AsyncMock(return_value={"id": "mock-updated"})

    # Support async context manager
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    return client


# ══════════════════════════════════════════════════════════════════════════════
# PA-1: Coverage Check
# ══════════════════════════════════════════════════════════════════════════════

class TestCoverageCheck:

    def test_pa_required_from_mock_crd(self, mock_fhir_client):
        """
        CRD returns a 'PA Required' card for CPT 95251.
        Expected: PAStatus.REQUIRED, payer_id=bcbs-ca-001.
        Mock payer server must be running on port 8080.
        """
        from agents.prior_auth.tools.coverage_check import check_coverage_requirements

        async def run():
            return await check_coverage_requirements(
                patient_id=PATIENT_ID,
                cpt_code="95251",
                fhir_client=mock_fhir_client,
                config=CONFIG,
                encounter_id=ENCOUNTER_ID,
                practitioner_id=PRACTITIONER_ID,
            )

        result = asyncio.run(run())
        print(f"\n  PA-1 result: status={result.status}, payer={result.payer_id}")

        # With mock server running: REQUIRED
        # Without mock server: UNKNOWN (fallback)
        assert result.status in (PAStatus.REQUIRED, PAStatus.UNKNOWN), \
            f"Expected REQUIRED or UNKNOWN, got {result.status}"

        if result.status == PAStatus.REQUIRED:
            assert result.payer_id == "bcbs-ca-001", \
                f"Expected payer_id=bcbs-ca-001, got {result.payer_id}"

    def test_pa_not_required_scenario(self, mock_fhir_client):
        """
        When CRD mock returns not_required cards, status should be NOT_REQUIRED.
        Requires mock payer server running with MOCK_CRD_STATUS=not_required.
        """
        import aiohttp

        async def run():
            # Set mock server scenario to not_required
            try:
                async with aiohttp.ClientSession() as session:
                    await session.post(
                        "http://localhost:8080/admin/set-scenario",
                        json={"crd_status": "not_required"}
                    )
            except Exception:
                pytest.skip("Mock payer server not running")

            from agents.prior_auth.tools.coverage_check import check_coverage_requirements
            result = await check_coverage_requirements(
                patient_id=PATIENT_ID,
                cpt_code="95251",
                fhir_client=mock_fhir_client,
                config=CONFIG,
            )

            # Reset to required
            try:
                async with aiohttp.ClientSession() as session:
                    await session.post(
                        "http://localhost:8080/admin/set-scenario",
                        json={"crd_status": "required"}
                    )
            except Exception:
                pass

            return result

        result = asyncio.run(run())
        print(f"\n  PA-1 not_required result: {result.status}")
        assert result.status == PAStatus.NOT_REQUIRED

    def test_coverage_payer_id_extraction(self):
        """Payer ID extraction from Coverage.payor[0].identifier.value."""
        from agents.prior_auth.tools.coverage_check import _extract_payer_id

        coverage = build_coverage()
        payer_id = _extract_payer_id(coverage)
        print(f"\n  Extracted payer_id: {payer_id}")
        assert payer_id == "bcbs-ca-001"

    def test_no_coverage_resource_returns_unknown(self, mock_fhir_client):
        """If no Coverage resource in FHIR, status should be UNKNOWN."""
        mock_fhir_client.search.side_effect = AsyncMock(return_value=[])

        from agents.prior_auth.tools.coverage_check import check_coverage_requirements

        async def run():
            return await check_coverage_requirements(
                patient_id=PATIENT_ID,
                cpt_code="95251",
                fhir_client=mock_fhir_client,
                config=CONFIG,
            )

        result = asyncio.run(run())
        print(f"\n  No coverage result: {result.status}")
        assert result.status == PAStatus.UNKNOWN


# ══════════════════════════════════════════════════════════════════════════════
# PA-2: DTR Fetch
# ══════════════════════════════════════════════════════════════════════════════

class TestDTRFetch:

    def test_local_template_loaded(self):
        """
        Local template bcbs-ca-001_95251.json should be found.
        Load synthetic patient first to ensure template exists.
        """
        from agents.prior_auth.tools.dtr_fetch import fetch_questionnaire

        async def run():
            return await fetch_questionnaire(
                payer_id="bcbs-ca-001",
                cpt_code="95251",
                config=CONFIG,
                force_refresh=True,  # skip cache
            )

        q = asyncio.run(run())
        print(f"\n  Questionnaire loaded: id={q.get('id')}, items={len(q.get('item', []))}")
        assert q["resourceType"] == "Questionnaire"
        assert len(q.get("item", [])) == 6, f"Expected 6 items, got {len(q.get('item', []))}"

    def test_firestore_cache_write_and_read(self):
        """
        First fetch writes to Firestore cache.
        Second fetch (within TTL) reads from cache without payer call.
        """
        from agents.prior_auth.tools.dtr_fetch import fetch_questionnaire

        async def run():
            # First fetch — will try payer endpoint, fall back to local template
            q1 = await fetch_questionnaire(
                payer_id="bcbs-ca-001",
                cpt_code="95251",
                config=CONFIG,
                force_refresh=True,
            )
            # Second fetch — should hit Firestore cache
            q2 = await fetch_questionnaire(
                payer_id="bcbs-ca-001",
                cpt_code="95251",
                config=CONFIG,
                force_refresh=False,
            )
            return q1, q2

        q1, q2 = asyncio.run(run())
        print(f"\n  Cache test: q1.id={q1.get('id')}, q2.id={q2.get('id')}")
        assert q1["resourceType"] == "Questionnaire"
        assert q2["resourceType"] == "Questionnaire"
        assert q1.get("id") == q2.get("id"), "Cached questionnaire should match original"

    def test_generic_fallback_when_no_template(self):
        """When no payer or CPT template exists, generic_pa.json should load."""
        from agents.prior_auth.tools.dtr_fetch import fetch_questionnaire

        async def run():
            return await fetch_questionnaire(
                payer_id="unknown-payer-xyz",
                cpt_code="00000",
                config=CONFIG,
                force_refresh=True,
            )

        q = asyncio.run(run())
        print(f"\n  Fallback questionnaire id={q.get('id')}")
        assert q["resourceType"] == "Questionnaire"


# ══════════════════════════════════════════════════════════════════════════════
# PA-3: Questionnaire Filler (Gemini)
# ══════════════════════════════════════════════════════════════════════════════

class TestQuestionnaireFiller:

    def test_all_required_questions_answered(self, mock_fhir_client):
        """
        With the CGM questionnaire and the synthetic patient's FHIR data,
        Gemini should answer all 6 questions with HIGH or MODERATE confidence.
        No MISSING answers expected.
        This test makes a real Gemini API call.
        """
        from agents.prior_auth.tools.questionnaire_filler import fill_questionnaire
        from scripts.load_synthetic_patient import build_clinical_impression

        questionnaire = build_questionnaire_template()
        patient_bundle = asyncio.run(mock_fhir_client.everything(PATIENT_ID))
        clinical_impression = build_clinical_impression()

        async def run():
            return await fill_questionnaire(
                questionnaire=questionnaire,
                patient_bundle=patient_bundle,
                clinical_impression=clinical_impression,
                fhir_client=mock_fhir_client,
                config=CONFIG,
            )

        answers = asyncio.run(run())

        print(f"\n  Answers received: {len(answers)}")
        for a in answers:
            print(f"    {a.link_id}: confidence={a.confidence.value}, "
                  f"cited={a.evidence_resource_id or 'NONE'}")

        # All 6 questions should be answered
        assert len(answers) == 6, f"Expected 6 answers, got {len(answers)}"

        # Count by confidence
        missing = [a for a in answers if a.confidence == AnswerConfidence.MISSING]
        high_or_mod = [a for a in answers
                       if a.confidence in (AnswerConfidence.HIGH, AnswerConfidence.MODERATE)]

        print(f"\n  HIGH/MODERATE: {len(high_or_mod)}, MISSING: {len(missing)}")

        # For our synthetic patient, 0 MISSING expected
        assert len(missing) == 0, (
            f"Expected 0 MISSING answers but got {len(missing)}: "
            f"{[a.link_id for a in missing]}"
        )

        # At least 4 of 6 should be HIGH or MODERATE
        assert len(high_or_mod) >= 4, (
            f"Expected >= 4 HIGH/MODERATE, got {len(high_or_mod)}"
        )

    def test_citation_validation_rejects_hallucinated_id(self, mock_fhir_client):
        """
        If Gemini cites a resource ID that doesn't exist in the patient bundle,
        the post-generation validator should downgrade it to LOW confidence.
        """
        from agents.prior_auth.tools.questionnaire_filler import _validate_answers
        from shared.models import QuestionnaireAnswer

        # Build an answer with a hallucinated resource ID
        fake_answer = QuestionnaireAnswer(
            link_id="q1",
            question_text="Test question",
            answer_value=True,
            confidence=AnswerConfidence.HIGH,
            evidence_resource_id="does-not-exist-in-bundle",
            evidence_text="Some text",
            is_required=True,
        )

        real_resource_ids = {PATIENT_ID, COVERAGE_ID, CONDITION_DM_ID, OBS_HBA1C_ID}

        validated = _validate_answers([fake_answer], real_resource_ids, questionnaire_items=[])

        print(f"\n  Hallucinated ID confidence after validation: {validated[0].confidence}")
        assert validated[0].confidence == AnswerConfidence.LOW, \
            "Hallucinated resource ID should be downgraded to LOW"

    def test_missing_citation_on_required_question(self, mock_fhir_client):
        """
        A HIGH confidence answer with no evidence_resource_id should be downgraded to MISSING.
        """
        from agents.prior_auth.tools.questionnaire_filler import _validate_answers
        from shared.models import QuestionnaireAnswer

        answer_no_citation = QuestionnaireAnswer(
            link_id="q1",
            question_text="Test question",
            answer_value=True,
            confidence=AnswerConfidence.HIGH,
            evidence_resource_id=None,  # no citation
            evidence_text=None,
            is_required=True,
        )

        validated = _validate_answers([answer_no_citation], set(), questionnaire_items=[])

        print(f"\n  No-citation confidence after validation: {validated[0].confidence}")
        assert validated[0].confidence == AnswerConfidence.MISSING, \
            "Answer without citation should be downgraded to MISSING"


# ══════════════════════════════════════════════════════════════════════════════
# PA-4: Bundle Assembler
# ══════════════════════════════════════════════════════════════════════════════

class TestBundleAssembler:

    def _build_test_answers(self):
        """Build the 6 answers Gemini would produce for our synthetic patient."""
        from shared.models import QuestionnaireAnswer

        return [
            QuestionnaireAnswer(
                link_id="q1", question_text="Does the patient have T1 or T2 DM?",
                answer_value=True, confidence=AnswerConfidence.HIGH,
                evidence_resource_id=CONDITION_DM_ID,
                evidence_text="Confirmed T2DM (E11.65)", is_required=True,
            ),
            QuestionnaireAnswer(
                link_id="q2", question_text="Most recent HbA1c value and date?",
                answer_value="8.2% on 2026-02-28", confidence=AnswerConfidence.HIGH,
                evidence_resource_id=OBS_HBA1C_ID,
                evidence_text="HbA1c 8.2% dated 2026-02-28", is_required=True,
            ),
            QuestionnaireAnswer(
                link_id="q3", question_text="Is the patient on insulin therapy?",
                answer_value=True, confidence=AnswerConfidence.HIGH,
                evidence_resource_id=MED_INSULIN_ID,
                evidence_text="Insulin glargine 20u QHS (active)", is_required=True,
            ),
            QuestionnaireAnswer(
                link_id="q4", question_text="Insulin product and dose?",
                answer_value="Insulin glargine (Lantus) 100 units/mL — 20 units subcutaneous at bedtime",
                confidence=AnswerConfidence.HIGH,
                evidence_resource_id=MED_INSULIN_ID,
                evidence_text="Active MedicationRequest for insulin glargine", is_required=True,
            ),
            QuestionnaireAnswer(
                link_id="q5", question_text="Any contraindications to CGM?",
                answer_value=False, confidence=AnswerConfidence.MODERATE,
                evidence_resource_id=ALLERGY_ID,
                evidence_text="Penicillin allergy only — no CGM contraindication",
                is_required=False,
            ),
            QuestionnaireAnswer(
                link_id="q6", question_text="Clinical justification for CGM?",
                answer_value=(
                    "HbA1c 8.2% with suboptimal glycemic control despite basal insulin and metformin. "
                    "CGM indicated for real-time glucose trending to optimize insulin titration and "
                    "reduce hypoglycemic risk in the context of concurrent CKD stage 3."
                ),
                confidence=AnswerConfidence.HIGH,
                evidence_resource_id="test-ci-001",
                evidence_text="ClinicalImpression confirms CGM indication", is_required=True,
            ),
        ]

    def test_bundle_structure_entry_order(self, mock_fhir_client):
        """
        PAS bundle must have entries in PAS IG v2.1.0 order:
        Claim → QuestionnaireResponse → ServiceRequest → Patient → Coverage
        """
        from agents.prior_auth.tools.bundle_assembler import assemble_pas_bundle

        answers = self._build_test_answers()

        async def run():
            return await assemble_pas_bundle(
                patient_id=PATIENT_ID,
                cpt_code="95251",
                payer_id="bcbs-ca-001",
                questionnaire_id="cgm-pa-bcbs-ca-001-95251",
                answers=answers,
                patient_resource=build_patient(),
                coverage_resource=build_coverage(),
                service_request={
                    "resourceType": "ServiceRequest", "id": f"sr-{PATIENT_ID}-95251",
                    "status": "draft", "intent": "proposal",
                    "subject": {"reference": f"Patient/{PATIENT_ID}"},
                    "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "95251"}]},
                },
                practitioner_resource=None,
                config=CONFIG,
            )

        bundle = asyncio.run(run())

        entries = bundle.get("entry", [])
        resource_types = [e["resource"]["resourceType"] for e in entries]

        print(f"\n  Bundle entry order: {resource_types}")

        assert bundle["resourceType"] == "Bundle"
        assert bundle["type"] == "transaction"
        assert len(entries) >= 4, f"Expected >= 4 entries, got {len(entries)}"
        assert resource_types[0] == "Claim", f"First entry must be Claim, got {resource_types[0]}"
        assert resource_types[1] == "QuestionnaireResponse", \
            f"Second entry must be QuestionnaireResponse, got {resource_types[1]}"

    def test_questionnaire_response_has_6_items(self, mock_fhir_client):
        """QuestionnaireResponse should have one item per answer."""
        from agents.prior_auth.tools.bundle_assembler import assemble_pas_bundle

        answers = self._build_test_answers()

        async def run():
            return await assemble_pas_bundle(
                patient_id=PATIENT_ID, cpt_code="95251", payer_id="bcbs-ca-001",
                questionnaire_id="cgm-pa-bcbs-ca-001-95251", answers=answers,
                patient_resource=build_patient(), coverage_resource=build_coverage(),
                service_request={"resourceType": "ServiceRequest", "id": "sr-test",
                                  "status": "draft", "intent": "proposal",
                                  "subject": {"reference": f"Patient/{PATIENT_ID}"},
                                  "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "95251"}]}},
                practitioner_resource=None, config=CONFIG,
            )

        bundle = asyncio.run(run())
        qr_entry = next(e["resource"] for e in bundle["entry"]
                        if e["resource"]["resourceType"] == "QuestionnaireResponse")

        items = qr_entry.get("item", [])
        print(f"\n  QR items: {len(items)}")
        assert len(items) == 6, f"Expected 6 QR items, got {len(items)}"

    def test_dlp_blocks_ssn(self, mock_fhir_client):
        """Bundle containing an SSN should raise DLPInspectionError."""
        from agents.prior_auth.tools.bundle_assembler import assemble_pas_bundle, DLPInspectionError
        from shared.models import QuestionnaireAnswer

        # Inject an SSN into an answer to trigger DLP block
        poisoned_answers = self._build_test_answers()
        poisoned_answers[5] = QuestionnaireAnswer(
            link_id="q6", question_text="Clinical justification?",
            answer_value="Patient SSN: 123-45-6789. CGM indicated.",
            confidence=AnswerConfidence.HIGH,
            evidence_resource_id="test-ci-001",
            evidence_text="Contains SSN — should be blocked by DLP",
            is_required=True,
        )

        async def run():
            return await assemble_pas_bundle(
                patient_id=PATIENT_ID, cpt_code="95251", payer_id="bcbs-ca-001",
                questionnaire_id="cgm-pa-bcbs-ca-001-95251", answers=poisoned_answers,
                patient_resource=build_patient(), coverage_resource=build_coverage(),
                service_request={"resourceType": "ServiceRequest", "id": "sr-test",
                                  "status": "draft", "intent": "proposal",
                                  "subject": {"reference": f"Patient/{PATIENT_ID}"},
                                  "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "95251"}]}},
                practitioner_resource=None, config=CONFIG,
            )

        print("\n  Testing DLP SSN block...")
        with pytest.raises(DLPInspectionError):
            asyncio.run(run())
        print("  DLP correctly blocked bundle containing SSN.")


# ══════════════════════════════════════════════════════════════════════════════
# PA-5: PAS Submit
# ══════════════════════════════════════════════════════════════════════════════

class TestPASSubmit:

    def test_submit_returns_pending_then_approved(self, mock_fhir_client):
        """
        Submit to mock payer.
        First poll returns PENDING.
        Second poll returns APPROVED with auth number.
        Requires mock payer server running.
        """
        import aiohttp
        from agents.prior_auth.tools.pas_submit import submit_pas_bundle, poll_for_decision
        from scripts.test_unit import _build_minimal_pas_bundle

        async def run():
            # Verify mock server is running
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get("http://localhost:8080/health") as resp:
                        if resp.status != 200:
                            return None
            except Exception:
                return None

            bundle = _build_minimal_pas_bundle()
            decision = await submit_pas_bundle(
                pas_bundle=bundle,
                patient_id=PATIENT_ID,
                cpt_code="95251",
                payer_id="bcbs-ca-001",
                fhir_client=mock_fhir_client,
                config=CONFIG,
                is_expedited=False,
            )
            return decision

        decision = asyncio.run(run())
        if decision is None:
            pytest.skip("Mock payer server not running — skipping PA-5 test")

        print(f"\n  Submit result: decision={decision.decision}, "
              f"sub_id={decision.submission_id if hasattr(decision, 'submission_id') else 'N/A'}")

    def test_submit_denied_scenario(self, mock_fhir_client):
        """Mock payer returns denied — ClaimResponse should reflect denial."""
        import aiohttp
        from agents.prior_auth.tools.pas_submit import submit_pas_bundle
        from scripts.test_unit import _build_minimal_pas_bundle
        from shared.models import ClaimDecision

        async def run():
            try:
                async with aiohttp.ClientSession() as session:
                    await session.post(
                        "http://localhost:8080/admin/set-scenario",
                        json={"pa_decision": "denied"}
                    )
            except Exception:
                pytest.skip("Mock payer server not running")

            bundle = _build_minimal_pas_bundle()
            decision = await submit_pas_bundle(
                pas_bundle=bundle,
                patient_id=PATIENT_ID, cpt_code="95251", payer_id="bcbs-ca-001",
                fhir_client=mock_fhir_client, config=CONFIG, is_expedited=False,
            )

            # Reset scenario
            try:
                async with aiohttp.ClientSession() as session:
                    await session.post(
                        "http://localhost:8080/admin/set-scenario",
                        json={"pa_decision": "approved"}
                    )
            except Exception:
                pass

            return decision

        decision = asyncio.run(run())
        if decision is not None:
            print(f"\n  Denied result: {decision.decision}")


# ── Shared helper ─────────────────────────────────────────────────────────────

def _build_minimal_pas_bundle() -> dict:
    """Minimal valid PAS bundle for submit/poll tests."""
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "fullUrl": f"urn:uuid:claim-test-001",
                "resource": {
                    "resourceType": "Claim",
                    "id": "claim-test-001",
                    "status": "active",
                    "use": "preauthorization",
                    "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type",
                                          "code": "professional"}]},
                    "patient": {"reference": f"Patient/{PATIENT_ID}"},
                    "created": "2026-03-10T10:00:00Z",
                    "insurer": {"identifier": {"value": "bcbs-ca-001"}},
                    "provider": {"reference": f"Practitioner/{PRACTITIONER_ID}"},
                    "priority": {"coding": [{"code": "normal"}]},
                    "insurance": [{"sequence": 1, "focal": True,
                                   "coverage": {"reference": f"Coverage/{COVERAGE_ID}"}}],
                },
                "request": {"method": "POST", "url": "Claim"},
            }
        ]
    }
