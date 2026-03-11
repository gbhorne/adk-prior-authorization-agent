"""
scripts/setup_payer_secrets.py
================================
Register payer CRD endpoints in Cloud Secret Manager.
The agent reads these at startup to know where to send CDS Hooks requests.

Usage:
    # Register mock payer (local dev/testing)
    python scripts/setup_payer_secrets.py --mode mock

    # Register real payer endpoints (production)
    python scripts/setup_payer_secrets.py --mode production

    # Add or update a single payer
    python scripts/setup_payer_secrets.py --add-payer bcbs-ca-001 https://api.bcbsca.com/crd/r4

    # List current registered payers
    python scripts/setup_payer_secrets.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import get_config


MOCK_ENDPOINTS = {
    "bcbs-ca-001": "http://localhost:8080/crd",
    "aetna-001":   "http://localhost:8080/crd",
    "uhc-001":     "http://localhost:8080/crd",
    "cigna-001":   "http://localhost:8080/crd",
}

# Replace with real Da Vinci-compliant payer endpoints when available
PRODUCTION_ENDPOINTS: dict[str, str] = {
    # "bcbs-ca-001": "https://api.bcbsca.com/crd/r4",
    # "aetna-001":   "https://prior-auth.aetna.com/crd/r4",
    # Example Da Vinci public test sandbox:
    "davinci-test": "https://prior-auth.davinci.hl7.org/crd",
}


def get_secret_manager_client():
    from google.cloud import secretmanager
    return secretmanager.SecretManagerServiceClient()


def secret_exists(client, project_id: str, secret_name: str) -> bool:
    try:
        name = f"projects/{project_id}/secrets/{secret_name}"
        client.get_secret(request={"name": name})
        return True
    except Exception:
        return False


def get_current_endpoints(client, project_id: str) -> dict:
    try:
        name = f"projects/{project_id}/secrets/pa-payer-endpoints/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return json.loads(response.payload.data.decode("utf-8"))
    except Exception:
        return {}


def write_endpoints(client, project_id: str, endpoints: dict) -> None:
    secret_name = "pa-payer-endpoints"
    payload = json.dumps(endpoints, indent=2).encode("utf-8")

    if not secret_exists(client, project_id, secret_name):
        # Create the secret first
        parent = f"projects/{project_id}"
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_name,
                "secret": {"replication": {"automatic": {}}},
            }
        )
        print(f"  Created secret: {secret_name}")

    # Add new version
    name = f"projects/{project_id}/secrets/{secret_name}"
    client.add_secret_version(
        request={"parent": name, "payload": {"data": payload}}
    )
    print(f"  Updated secret version: {secret_name}")


def list_payers(project_id: str) -> None:
    client = get_secret_manager_client()
    endpoints = get_current_endpoints(client, project_id)

    if not endpoints:
        print("  No payer endpoints registered yet.")
        return

    print(f"\n  Registered payers ({len(endpoints)}):")
    for payer_id, url in endpoints.items():
        print(f"    {payer_id:<20} {url}")


def setup_mock(project_id: str) -> None:
    client = get_secret_manager_client()
    print(f"\n  Registering mock payer endpoints (localhost:8080)...")
    write_endpoints(client, project_id, MOCK_ENDPOINTS)
    print(f"  Done. {len(MOCK_ENDPOINTS)} mock payers registered.")
    print()
    print("  These point to the local mock payer server.")
    print("  Start it with: python scripts/mock_payer_server.py")


def setup_production(project_id: str) -> None:
    if not PRODUCTION_ENDPOINTS:
        print("  No production endpoints configured.")
        print("  Edit PRODUCTION_ENDPOINTS in this script to add real payer URLs.")
        return

    client = get_secret_manager_client()
    print(f"\n  Registering production payer endpoints...")
    write_endpoints(client, project_id, PRODUCTION_ENDPOINTS)
    print(f"  Done. {len(PRODUCTION_ENDPOINTS)} production payers registered.")


def add_payer(project_id: str, payer_id: str, url: str) -> None:
    client = get_secret_manager_client()
    current = get_current_endpoints(client, project_id)
    current[payer_id] = url
    write_endpoints(client, project_id, current)
    print(f"  Payer '{payer_id}' registered: {url}")


def main() -> None:
    config = get_config()
    project_id = config.gcp_project_id

    parser = argparse.ArgumentParser(description="Setup PA Agent payer endpoints in Secret Manager")
    parser.add_argument("--mode", choices=["mock", "production"],
                        help="Register all mock or production endpoints")
    parser.add_argument("--add-payer", nargs=2, metavar=("PAYER_ID", "URL"),
                        help="Add or update a single payer endpoint")
    parser.add_argument("--list", action="store_true", help="List registered payers")
    args = parser.parse_args()

    print()
    print("=" * 55)
    print("  PA Agent — Payer Endpoint Setup")
    print(f"  Project: {project_id}")
    print("=" * 55)

    if args.list:
        list_payers(project_id)
    elif args.mode == "mock":
        setup_mock(project_id)
    elif args.mode == "production":
        setup_production(project_id)
    elif args.add_payer:
        payer_id, url = args.add_payer
        add_payer(project_id, payer_id, url)
    else:
        # Default: show current state + usage
        list_payers(project_id)
        print()
        print("  Usage:")
        print("    python scripts/setup_payer_secrets.py --mode mock")
        print("    python scripts/setup_payer_secrets.py --mode production")
        print("    python scripts/setup_payer_secrets.py --add-payer bcbs-ca-001 https://api.bcbsca.com/crd")
        print("    python scripts/setup_payer_secrets.py --list")
    print()


if __name__ == "__main__":
    main()
