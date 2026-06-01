# SPDX-License-Identifier: FSL-1.1-MIT
import json
import subprocess
from collections.abc import Mapping
from typing import Any

from django.conf import settings


class KaspaPstError(Exception):
    pass


class KaspaPstClient:
    """
    Thin subprocess adapter for a Kaspa PST helper binary.

    Helper contract:
    - command is argv[1]: inspect, merge, broadcast
    - request is JSON on stdin
    - response is JSON on stdout
    - non-zero exit status means validation/helper failure
    """

    def __init__(self, helper_path: str | None = None, timeout: int | None = None):
        self.helper_path = helper_path or settings.KASPA_PST_HELPER_PATH
        self.timeout = timeout or settings.KASPA_PST_HELPER_TIMEOUT

    def inspect(self, bundle_hex: str, network: str) -> dict[str, Any]:
        return self._run(
            "inspect",
            {
                "network": network,
                "bundleHex": bundle_hex,
            },
        )

    def merge(
        self,
        current_bundle_hex: str,
        signed_bundle_hex: str,
        network: str,
    ) -> dict[str, Any]:
        return self._run(
            "merge",
            {
                "network": network,
                "currentBundleHex": current_bundle_hex,
                "signedBundleHex": signed_bundle_hex,
            },
        )

    def broadcast(
        self,
        bundle_hex: str,
        network: str,
        rpc_url: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "network": network,
            "bundleHex": bundle_hex,
        }
        if rpc_url:
            payload["rpcUrl"] = rpc_url
        return self._run("broadcast", payload)

    def _run(self, command: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            completed_process = subprocess.run(
                [self.helper_path, command],
                input=json.dumps(payload),
                capture_output=True,
                check=False,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise KaspaPstError(
                f"Kaspa PST helper not found at {self.helper_path!r}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise KaspaPstError(
                f"Kaspa PST helper timed out after {self.timeout} seconds"
            ) from exc

        if completed_process.returncode != 0:
            message = completed_process.stderr.strip() or completed_process.stdout.strip()
            raise KaspaPstError(message or "Kaspa PST helper failed")

        try:
            result = json.loads(completed_process.stdout)
        except json.JSONDecodeError as exc:
            raise KaspaPstError("Kaspa PST helper returned invalid JSON") from exc

        if not isinstance(result, dict):
            raise KaspaPstError("Kaspa PST helper returned a non-object JSON response")

        return result


def get_pst_client() -> KaspaPstClient:
    return KaspaPstClient()
