# SPDX-License-Identifier: FSL-1.1-MIT
import hashlib
import json
import re
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.db import IntegrityError, transaction

from rest_framework.exceptions import ValidationError

from safe_transaction_service.kaspa.models import (
    KaspaExitBatch,
    KaspaExitBatchStatus,
    KaspaExitRequest,
    KaspaFederation,
    KaspaTxProposal,
)
from safe_transaction_service.kaspa.serializers import (
    KaspaTxProposalCreateSerializer,
    canonical_json_hash,
)


class KaspaExitProposalBuilderError(Exception):
    pass


@dataclass(frozen=True)
class KebBundleRunnerConfig:
    config_path: str | None = None
    reports_dir: str | None = None
    runner_cwd: str | None = None
    runner_command: list[str] | None = None
    delta_blocks: int = 86_400
    start_block: int | None = None
    end_block: int | None = None
    previous_checkpoint_file: str | None = None
    contract_expected_values_file: str | None = None
    manifest_signing_private_key: str | None = None
    manifest_signing_public_key: str | None = None
    manifest_signing_key_id: str | None = None
    manifest_signing_key_type: str | None = None
    cleanup_created_outside_bundle: bool = True
    timeout: int = 1_800


@dataclass(frozen=True)
class KaspaUtxoSelectorConfig:
    rpc_url: str | None = None
    api_url: str | None = None
    helper_command: list[str] | None = None
    timeout: int = 60
    coinbase_maturity_daa: int = 1_000
    min_confirmations_daa: int = 0
    min_amount_sompi: int = 0
    max_inputs: int = 64


@dataclass(frozen=True)
class KaspaExitProposalBuilderConfig:
    network: str
    l2_chain_id: int
    igra_rpc_url: str
    kas_exit_bridge: str
    mailbox: str
    merkle_tree_hook: str
    canonical_bridge_address: str
    canonical_bridge_script_public_key: str
    canonical_derivation_path: str = "m/0/0/1"
    kaspa_tx_id_prefix: str = "97b1"
    l2_confirmation_blocks: int = 12
    proposed_by: str = "igra-exit-proposal-builder"
    foundry_extended_public_keys: list[str] | None = None
    keb: KebBundleRunnerConfig | None = None
    kaspa_utxos: KaspaUtxoSelectorConfig | None = None


@dataclass(frozen=True)
class KaspaExitProposalBuildResult:
    exit_batch: KaspaExitBatch
    proposal: KaspaTxProposal | None
    evidence_hash: str
    unsigned_manifest: dict[str, Any]
    unsigned_bundle_hex: str


_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_ALPHABET_BYTES = _BASE58_ALPHABET.encode()
_BASE58_INDEX = {char: index for index, char in enumerate(_BASE58_ALPHABET)}
_BASE58_XPUB_RE = re.compile(
    rb"(?=([" + re.escape(_BASE58_ALPHABET_BYTES) + rb"]{111}))"
)
_KASPA_PUBLIC_XPUB_VERSIONS = {
    "mainnet": bytes.fromhex("038f332e"),
    "testnet": bytes.fromhex("0390a241"),
    "devnet": bytes.fromhex("038b41ba"),
    "simnet": bytes.fromhex("0390467d"),
}


def load_builder_config(path: str | Path) -> KaspaExitProposalBuilderConfig:
    data = _load_json(Path(path))
    contracts = _expect_dict(data.get("contracts"), "contracts")
    bridge = _expect_dict(data.get("bridge"), "bridge")
    kaspa = data.get("kaspa") or {}
    if not isinstance(kaspa, dict):
        raise KaspaExitProposalBuilderError("kaspa must be an object")
    foundry_extended_public_keys = data.get("foundryExtendedPublicKeys")
    if foundry_extended_public_keys is not None:
        foundry_extended_public_keys = _expect_str_list(
            foundry_extended_public_keys,
            "foundryExtendedPublicKeys",
        )

    return KaspaExitProposalBuilderConfig(
        network=_expect_str(data.get("network"), "network"),
        l2_chain_id=int(data["l2ChainId"]),
        igra_rpc_url=_expect_str(data.get("igraRpcUrl"), "igraRpcUrl"),
        kas_exit_bridge=_expect_str(contracts.get("kasExitBridge"), "contracts.kasExitBridge"),
        mailbox=_expect_str(contracts.get("mailbox"), "contracts.mailbox"),
        merkle_tree_hook=_expect_str(
            contracts.get("merkleTreeHook"), "contracts.merkleTreeHook"
        ),
        canonical_bridge_address=_expect_str(
            bridge.get("address"), "bridge.address"
        ),
        canonical_bridge_script_public_key=_normalize_hex(
            _expect_str(bridge.get("scriptPublicKey"), "bridge.scriptPublicKey")
        ),
        canonical_derivation_path=str(
            bridge.get("derivationPath") or "m/0/0/1"
        ),
        kaspa_tx_id_prefix=_normalize_hex(
            str(data.get("kaspaTxIdPrefix") or "97b1")
        ),
        l2_confirmation_blocks=int(data.get("l2ConfirmationBlocks", 12)),
        proposed_by=str(data.get("proposedBy") or "igra-exit-proposal-builder"),
        foundry_extended_public_keys=foundry_extended_public_keys,
        keb=_parse_keb_runner_config(data.get("keb")),
        kaspa_utxos=_parse_kaspa_utxo_selector_config(
            data.get("kaspaUtxos") or kaspa.get("utxos"),
            default_rpc_url=kaspa.get("rpcUrl") or data.get("kaspaRpcUrl"),
        ),
    )


def _parse_keb_runner_config(value: Any) -> KebBundleRunnerConfig | None:
    if value is None:
        return None
    data = _expect_dict(value, "keb")
    return KebBundleRunnerConfig(
        config_path=data.get("configPath") or data.get("config"),
        reports_dir=data.get("reportsDir"),
        runner_cwd=data.get("runnerCwd") or data.get("cwd"),
        runner_command=_parse_command(data.get("runnerCommand") or data.get("command")),
        delta_blocks=int(data.get("deltaBlocks", 86_400)),
        start_block=_optional_int(data.get("startBlock")),
        end_block=_optional_int(data.get("endBlock")),
        previous_checkpoint_file=data.get("previousCheckpointFile"),
        contract_expected_values_file=data.get("contractExpectedValuesFile"),
        manifest_signing_private_key=data.get("manifestSigningPrivateKey"),
        manifest_signing_public_key=data.get("manifestSigningPublicKey"),
        manifest_signing_key_id=data.get("manifestSigningKeyId"),
        manifest_signing_key_type=data.get("manifestSigningKeyType"),
        cleanup_created_outside_bundle=bool(
            data.get("cleanupCreatedOutsideBundle", True)
        ),
        timeout=int(data.get("timeout", 1_800)),
    )


def _parse_kaspa_utxo_selector_config(
    value: Any,
    *,
    default_rpc_url: str | None,
) -> KaspaUtxoSelectorConfig | None:
    if value is None and not default_rpc_url:
        return None
    data = _expect_dict(value or {}, "kaspaUtxos")
    rpc_url = data.get("rpcUrl") or default_rpc_url
    helper_command = _parse_command(data.get("helperCommand"))
    if rpc_url and not helper_command and not data.get("apiUrl"):
        helper_command = ["kaspa-pst", "utxos"]
    return KaspaUtxoSelectorConfig(
        rpc_url=rpc_url,
        api_url=data.get("apiUrl"),
        helper_command=helper_command,
        timeout=int(data.get("timeout", 60)),
        coinbase_maturity_daa=int(data.get("coinbaseMaturityDaa", 1_000)),
        min_confirmations_daa=int(data.get("minConfirmationsDaa", 0)),
        min_amount_sompi=int(data.get("minAmountSompi", 0)),
        max_inputs=int(data.get("maxInputs", 64)),
    )


def _parse_command(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [_expect_str(item, "command[]") for item in value]
    if isinstance(value, str):
        parts = shlex.split(value)
        if not parts:
            raise KaspaExitProposalBuilderError("command cannot be empty")
        return parts
    raise KaspaExitProposalBuilderError("command must be a string or array")


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


class L2FinalityClient:
    def __init__(self, rpc_url: str, timeout: int = 10):
        self.rpc_url = rpc_url
        self.timeout = timeout

    def is_ready(self, to_block: int, confirmations: int) -> bool:
        finalized = self._block_number("finalized")
        if finalized is not None and finalized >= to_block:
            return True

        latest = self._block_number("latest")
        if latest is None:
            return False
        return latest >= to_block + confirmations

    def _block_number(self, tag: str) -> int | None:
        try:
            result = self._rpc(
                "eth_getBlockByNumber",
                [tag, False],
            )
        except KaspaExitProposalBuilderError:
            if tag == "finalized":
                return None
            raise
        if result is None:
            return None
        number = result.get("number") if isinstance(result, dict) else None
        if not number:
            return None
        return int(number, 16)

    def _rpc(self, method: str, params: list[Any]) -> Any:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        request = urllib.request.Request(
            self.rpc_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise KaspaExitProposalBuilderError(
                f"failed to query Igra RPC {self.rpc_url}: {exc}"
            ) from exc
        data = json.loads(body)
        if data.get("error"):
            raise KaspaExitProposalBuilderError(
                f"Igra RPC {method} failed: {data['error']}"
            )
        return data.get("result")


class FoundryExitBuilder:
    def __init__(
        self,
        cast_bin: str = "cast",
        timeout: int = 180,
        allow_non_igra_lock_script_for_testing: bool = False,
    ):
        self.cast_bin = cast_bin
        self.timeout = timeout
        self.allow_non_igra_lock_script_for_testing = (
            allow_non_igra_lock_script_for_testing
        )

    def build_exit(
        self,
        *,
        network: str,
        tx_id_prefix: str,
        build_input: dict[str, Any],
        mining_timeout_secs: int,
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="kaspa-exit-builder-") as tmp:
            tmp_path = Path(tmp)
            input_path = tmp_path / "exit.input.json"
            manifest_path = tmp_path / "exit.unsigned.json"
            hex_path = tmp_path / "exit.unsigned.hex"
            input_path.write_text(
                json.dumps(strip_builder_only_build_input_fields(build_input), indent=2)
                + "\n"
            )

            cmd = [
                self.cast_bin,
                "igra",
                "build-exit",
                "--network",
                network,
                "--tx-id-prefix",
                tx_id_prefix,
                "--input",
                str(input_path),
                "--out-json",
                str(manifest_path),
                "--out-hex",
                str(hex_path),
                "--mining-timeout-secs",
                str(mining_timeout_secs),
                "--force",
            ]
            if self.allow_non_igra_lock_script_for_testing:
                cmd.append("--allow-non-igra-lock-script-for-testing")
            self._run(cmd)

            manifest = _load_json(manifest_path)
            wallet_hex = hex_path.read_text().strip()
            verify_report = self.verify_exit(
                manifest_path=manifest_path,
                hex_path=hex_path,
            )
            return manifest, wallet_hex, verify_report

    def verify_exit(self, *, manifest_path: Path, hex_path: Path) -> dict[str, Any]:
        cmd = [
            self.cast_bin,
            "igra",
            "verify-exit",
            "--manifest",
            str(manifest_path),
            "--hex",
            str(hex_path),
        ]
        if self.allow_non_igra_lock_script_for_testing:
            cmd.append("--allow-non-igra-lock-script-for-testing")
        completed = self._run(cmd)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise KaspaExitProposalBuilderError(
                "cast igra verify-exit returned invalid JSON"
            ) from exc

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise KaspaExitProposalBuilderError(
                f"Foundry cast binary not found: {self.cast_bin}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise KaspaExitProposalBuilderError(
                f"Foundry command timed out after {self.timeout}s: {' '.join(cmd)}"
            ) from exc
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise KaspaExitProposalBuilderError(
                message or f"Foundry command failed: {' '.join(cmd)}"
            )
        return completed


class KebBundleRunner:
    BUNDLE_RE = re.compile(r"^keb-from-(\d+)-to-(\d+)-(\d{8}T\d{6}Z)\.bundle$")

    def __init__(self, config: KebBundleRunnerConfig):
        self.config = config

    def next_window(self) -> tuple[int, int] | None:
        if self.config.start_block is not None:
            to_block = (
                self.config.end_block
                if self.config.end_block is not None
                else self.config.start_block + self.config.delta_blocks - 1
            )
            return self.config.start_block, to_block

        latest = self._latest_bundle()
        if latest is None:
            return None
        from_block = latest[1] + 1
        to_block = (
            self.config.end_block
            if self.config.end_block is not None
            else from_block + self.config.delta_blocks - 1
        )
        return from_block, to_block

    def run_delta(self) -> Path:
        if not self.config.reports_dir:
            raise KaspaExitProposalBuilderError(
                "KEB reports_dir is required to create a bundle"
            )

        reports_dir = Path(self.config.reports_dir)
        reports_dir.mkdir(parents=True, exist_ok=True)
        started_at = time.time()
        cmd = list(self.config.runner_command or ["npm", "run", "kas-exit:run-delta", "--"])
        cmd.extend(["--reports-dir", str(reports_dir), "--runs", "1"])

        if self.config.config_path:
            cmd.extend(["--config", self.config.config_path])
        if self.config.delta_blocks:
            cmd.extend(["--delta-blocks", str(self.config.delta_blocks)])
        if self.config.start_block is not None:
            cmd.extend(["--start-block", str(self.config.start_block)])
        if self.config.end_block is not None:
            cmd.extend(["--end-block", str(self.config.end_block)])
        if self.config.previous_checkpoint_file:
            cmd.extend(["--previous-checkpoint-file", self.config.previous_checkpoint_file])
        if self.config.contract_expected_values_file:
            cmd.extend(
                [
                    "--contract-expected-values-file",
                    self.config.contract_expected_values_file,
                ]
            )
        if self.config.manifest_signing_private_key:
            cmd.extend(
                [
                    "--manifest-signing-private-key",
                    self.config.manifest_signing_private_key,
                ]
            )
        if self.config.manifest_signing_public_key:
            cmd.extend(
                [
                    "--manifest-signing-public-key",
                    self.config.manifest_signing_public_key,
                ]
            )
        if self.config.manifest_signing_key_id:
            cmd.extend(["--manifest-signing-key-id", self.config.manifest_signing_key_id])
        if self.config.manifest_signing_key_type:
            cmd.extend(
                ["--manifest-signing-key-type", self.config.manifest_signing_key_type]
            )
        if self.config.cleanup_created_outside_bundle:
            cmd.append("--cleanup-created-outside-bundle")

        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                cwd=self.config.runner_cwd,
                text=True,
                timeout=self.config.timeout,
            )
        except FileNotFoundError as exc:
            raise KaspaExitProposalBuilderError(
                f"KEB runner command not found: {cmd[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise KaspaExitProposalBuilderError(
                f"KEB runner timed out after {self.config.timeout}s"
            ) from exc
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise KaspaExitProposalBuilderError(
                message or f"KEB runner failed: {' '.join(cmd)}"
            )

        candidates = self._bundles_created_since(started_at - 1)
        if not candidates:
            latest = self._latest_bundle()
            if latest is None:
                raise KaspaExitProposalBuilderError(
                    "KEB runner succeeded but no bundle was found"
                )
            return latest[2]
        return sorted(candidates, key=lambda item: item[2].stat().st_mtime)[-1][2]

    def _latest_bundle(self) -> tuple[int, int, Path] | None:
        bundles = self._discover_bundles()
        if not bundles:
            return None
        return sorted(bundles, key=lambda item: (item[1], item[0], item[2].name))[-1]

    def _bundles_created_since(self, timestamp: float) -> list[tuple[int, int, Path]]:
        return [item for item in self._discover_bundles() if item[2].stat().st_mtime >= timestamp]

    def _discover_bundles(self) -> list[tuple[int, int, Path]]:
        if not self.config.reports_dir:
            return []
        reports_dir = Path(self.config.reports_dir)
        if not reports_dir.is_dir():
            return []

        out: list[tuple[int, int, Path]] = []
        for path in reports_dir.iterdir():
            if not path.is_dir():
                continue
            match = self.BUNDLE_RE.match(path.name)
            if not match:
                continue
            out.append((int(match.group(1)), int(match.group(2)), path))
        return out


class KaspaRpcUtxoSelector:
    def __init__(self, config: KaspaUtxoSelectorConfig):
        self.config = config

    def select_locking_utxos(
        self,
        *,
        network: str,
        bridge_address: str,
        bridge_script_public_key: str,
        derivation_path: str,
        required_sompi: int,
    ) -> list[dict[str, Any]]:
        report = self.query_address_utxos(
            network=network,
            bridge_address=bridge_address,
            bridge_script_public_key=bridge_script_public_key,
        )
        current_daa = int(report["virtualDaaScore"])
        normalized = [
            self._normalize_entry(
                entry,
                bridge_address=bridge_address,
                bridge_script_public_key=bridge_script_public_key,
                derivation_path=derivation_path,
                current_daa=current_daa,
            )
            for entry in report["entries"]
        ]
        candidates = [entry for entry in normalized if entry["selection"]["eligible"]]
        candidates.sort(
            key=lambda item: (
                item["selection"]["isCoinbase"],
                item["selection"]["blockDaaScore"],
                -item["bridge_utxo"]["amount_sompi"],
                item["bridge_utxo"]["transaction_id"],
                item["bridge_utxo"]["output_index"],
            )
        )

        selected: list[dict[str, Any]] = []
        total = 0
        for entry in candidates:
            if len(selected) >= self.config.max_inputs:
                break
            selected.append(entry)
            total += entry["bridge_utxo"]["amount_sompi"]
            if total >= required_sompi:
                break

        if total < required_sompi:
            raise KaspaExitProposalBuilderError(
                "not enough mature bridge UTXOs for exit proposal: "
                f"required {required_sompi} sompi, selected {total} sompi, "
                f"eligible {len(candidates)} of {len(normalized)} UTXOs, "
                f"max inputs {self.config.max_inputs}"
            )
        return selected

    def query_address_utxos(
        self,
        *,
        network: str,
        bridge_address: str,
        bridge_script_public_key: str,
    ) -> dict[str, Any]:
        if self.config.helper_command:
            return self._query_with_helper(
                network=network,
                bridge_address=bridge_address,
                bridge_script_public_key=bridge_script_public_key,
            )
        if self.config.api_url:
            return self._query_with_http_api(bridge_address)
        raise KaspaExitProposalBuilderError(
            "Kaspa UTXO selector requires kaspaUtxos.helperCommand or kaspaUtxos.apiUrl"
        )

    def _query_with_helper(
        self,
        *,
        network: str,
        bridge_address: str,
        bridge_script_public_key: str,
    ) -> dict[str, Any]:
        if not self.config.rpc_url:
            raise KaspaExitProposalBuilderError(
                "kaspaUtxos.rpcUrl is required when using a Kaspa RPC helper"
            )
        payload = {
            "network": network,
            "rpcUrl": self.config.rpc_url,
            "address": bridge_address,
            "scriptPublicKey": bridge_script_public_key,
            "coinbaseMaturityDaa": self.config.coinbase_maturity_daa,
            "minConfirmationsDaa": self.config.min_confirmations_daa,
            "minAmountSompi": self.config.min_amount_sompi,
            "maxInputs": self.config.max_inputs,
        }
        try:
            completed = subprocess.run(
                self.config.helper_command,
                input=json.dumps(payload),
                capture_output=True,
                check=False,
                text=True,
                timeout=self.config.timeout,
            )
        except FileNotFoundError as exc:
            raise KaspaExitProposalBuilderError(
                f"Kaspa UTXO helper not found: {self.config.helper_command[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise KaspaExitProposalBuilderError(
                f"Kaspa UTXO helper timed out after {self.config.timeout}s"
            ) from exc
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise KaspaExitProposalBuilderError(
                message or "Kaspa UTXO helper failed"
            )
        return self._validate_utxo_report(completed.stdout, source="kaspa-rpc-helper")

    def _query_with_http_api(self, bridge_address: str) -> dict[str, Any]:
        base = self.config.api_url.rstrip("/")
        url = f"{base}/addresses/{urllib.parse.quote(bridge_address, safe='')}/utxos"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                body = response.read().decode()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise KaspaExitProposalBuilderError(
                f"failed to query Kaspa UTXO API {url}: {exc}"
            ) from exc
        data = json.loads(body)
        if not isinstance(data, list):
            raise KaspaExitProposalBuilderError("Kaspa UTXO API returned non-array JSON")
        return {
            "source": "kaspa-http-api",
            "address": bridge_address,
            "checkedAt": _utc_now(),
            "virtualDaaScore": 0,
            "entries": data,
        }

    def _validate_utxo_report(self, raw: str, *, source: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KaspaExitProposalBuilderError(
                "Kaspa UTXO helper returned invalid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise KaspaExitProposalBuilderError("Kaspa UTXO helper returned non-object JSON")
        if "entries" not in data or not isinstance(data["entries"], list):
            raise KaspaExitProposalBuilderError(
                "Kaspa UTXO helper response must contain entries array"
            )
        data.setdefault("source", source)
        data.setdefault("checkedAt", _utc_now())
        if "virtualDaaScore" not in data:
            raise KaspaExitProposalBuilderError(
                "Kaspa UTXO helper response must contain virtualDaaScore"
            )
        return data

    def _normalize_entry(
        self,
        entry: dict[str, Any],
        *,
        bridge_address: str,
        bridge_script_public_key: str,
        derivation_path: str,
        current_daa: int,
    ) -> dict[str, Any]:
        outpoint = _expect_dict(entry.get("outpoint"), "utxo.outpoint")
        utxo_entry = _expect_dict(entry.get("utxoEntry"), "utxo.utxoEntry")
        script_public_key = _expect_dict(
            utxo_entry.get("scriptPublicKey"), "utxo.utxoEntry.scriptPublicKey"
        )
        transaction_id = _expect_str(
            outpoint.get("transactionId") or outpoint.get("transaction_id"),
            "utxo.outpoint.transactionId",
        )
        output_index = int(outpoint.get("index"))
        amount_sompi = int(utxo_entry.get("amount"))
        script = _normalize_hex(
            str(script_public_key.get("script") or script_public_key.get("scriptPublicKey"))
        )
        is_coinbase = bool(utxo_entry.get("isCoinbase", utxo_entry.get("is_coinbase", False)))
        block_daa_score = int(
            utxo_entry.get("blockDaaScore", utxo_entry.get("block_daa_score", 0))
        )
        script_matches = script == _normalize_hex(bridge_script_public_key)
        address_matches = not entry.get("address") or entry.get("address") == bridge_address
        confirmations_daa = max(0, current_daa - block_daa_score)
        confirmed = confirmations_daa >= self.config.min_confirmations_daa
        mature = (
            True
            if not is_coinbase
            else current_daa >= block_daa_score + self.config.coinbase_maturity_daa
        )
        enough_amount = amount_sompi >= self.config.min_amount_sompi
        eligible = script_matches and address_matches and mature and confirmed and enough_amount
        selected_at = _utc_now()
        return {
            "utxo_id": f"{transaction_id}:{output_index}",
            "bridge_utxo": {
                "transaction_id": transaction_id,
                "output_index": output_index,
                "amount_sompi": amount_sompi,
                "script_public_key": script,
            },
            "live_api_utxo": entry,
            "selection": {
                "source": "kaspa-node-rpc",
                "selectedAt": selected_at,
                "virtualDaaScore": current_daa,
                "coinbaseMaturityDaa": self.config.coinbase_maturity_daa,
                "minConfirmationsDaa": self.config.min_confirmations_daa,
                "minAmountSompi": self.config.min_amount_sompi,
                "isCoinbase": is_coinbase,
                "blockDaaScore": block_daa_score,
                "confirmationsDaa": confirmations_daa,
                "mature": mature,
                "confirmed": confirmed,
                "amountAccepted": enough_amount,
                "scriptMatches": script_matches,
                "addressMatches": address_matches,
                "eligible": eligible,
                "requiredScriptPublicKey": _normalize_hex(bridge_script_public_key),
            },
            "address": bridge_address,
            "derivation_path": derivation_path,
        }


class KaspaExitProposalBuilder:
    def __init__(
        self,
        *,
        config: KaspaExitProposalBuilderConfig,
        federation: KaspaFederation,
    ):
        self.config = config
        self.federation = federation

    def build_from_artifacts(
        self,
        *,
        bundle_dir: str | Path,
        unsigned_manifest: dict[str, Any],
        unsigned_bundle_hex: str,
        unsigned_verify_report: dict[str, Any],
        build_input: dict[str, Any] | None = None,
        submit_proposal: bool = True,
    ) -> KaspaExitProposalBuildResult:
        bundle = load_keb_bundle(bundle_dir)
        self._validate_federation()
        exit_requests = self._validate_and_extract_exits(bundle)
        self._validate_unsigned_manifest(unsigned_manifest, unsigned_verify_report)
        unsigned_bundle_hex, wallet_hex_normalization = normalize_pst_xpub_versions(
            unsigned_bundle_hex,
            self.config.network,
        )

        evidence = self._build_evidence(
            bundle=bundle,
            exit_requests=exit_requests,
            build_input=build_input,
        )
        evidence_hash = canonical_json_hash(evidence)
        artifact_hashes = dict(bundle.manifest.get("artifactChecksums") or {})
        proposal_artifact_hashes = {
            "unsignedManifestSha256": _sha256_json(unsigned_manifest),
            "unsignedBundleHexSha256": hashlib.sha256(
                unsigned_bundle_hex.encode()
            ).hexdigest(),
        }
        if wallet_hex_normalization and wallet_hex_normalization.get("applied"):
            proposal_artifact_hashes["preNormalizationUnsignedBundleHexSha256"] = (
                wallet_hex_normalization["originalBundleHexSha256"]
            )
        if build_input:
            proposal_artifact_hashes["buildInputSha256"] = _sha256_json(build_input)

        exit_batch = self._get_or_create_exit_batch(
            bundle=bundle,
            exit_requests=exit_requests,
            evidence=evidence,
            evidence_hash=evidence_hash,
            artifact_hashes=artifact_hashes,
        )

        proposal = None
        if submit_proposal:
            origin = {
                "kind": "igra-l2-exit",
                "builder": "safe-transaction-service",
                "evidenceHash": evidence_hash,
                "candidate": {
                    "artifactHashes": proposal_artifact_hashes,
                    "buildInput": build_input,
                    "unsignedManifest": unsigned_manifest,
                    "unsignedVerify": unsigned_verify_report,
                    "walletHexNormalization": wallet_hex_normalization,
                },
            }
            serializer = KaspaTxProposalCreateSerializer(
                data={
                    "unsigned_bundle_hex": unsigned_bundle_hex,
                    "exit_batch": str(exit_batch.pk),
                    "proposed_by": self.config.proposed_by,
                    "origin": origin,
                },
                context={"federation": self.federation},
            )
            try:
                serializer.is_valid(raise_exception=True)
                proposal = serializer.save()
            except ValidationError as exc:
                raise KaspaExitProposalBuilderError(str(exc.detail)) from exc

        return KaspaExitProposalBuildResult(
            exit_batch=exit_batch,
            proposal=proposal,
            evidence_hash=evidence_hash,
            unsigned_manifest=unsigned_manifest,
            unsigned_bundle_hex=unsigned_bundle_hex,
        )

    def _get_or_create_exit_batch(
        self,
        *,
        bundle: "KebBundle",
        exit_requests: list[dict[str, Any]],
        evidence: dict[str, Any],
        evidence_hash: str,
        artifact_hashes: dict[str, str],
    ) -> KaspaExitBatch:
        try:
            with transaction.atomic():
                exit_batch = KaspaExitBatch.objects.create(
                    federation=self.federation,
                    network=self.config.network,
                    l2_chain_id=self.config.l2_chain_id,
                    from_block=bundle.from_block,
                    to_block=bundle.to_block,
                    finalized_at_block=bundle.to_block,
                    status=KaspaExitBatchStatus.VERIFIED,
                    evidence_hash=evidence_hash,
                    total_exits=len(exit_requests),
                    total_amount_sompi=sum(
                        request["amount_sompi"] for request in exit_requests
                    ),
                    canonical_bridge_address=self.config.canonical_bridge_address,
                    canonical_bridge_script_public_key=(
                        self.config.canonical_bridge_script_public_key
                    ),
                    canonical_derivation_path=self.config.canonical_derivation_path,
                    threshold=self.federation.threshold,
                    xpub_fingerprint=self.federation.xpub_fingerprint,
                    checks=self._checks_summary(bundle),
                    artifact_hashes=artifact_hashes,
                    evidence=evidence,
                )
                KaspaExitRequest.objects.bulk_create(
                    [
                        KaspaExitRequest(batch=exit_batch, **request)
                        for request in exit_requests
                    ]
                )
                return exit_batch
        except IntegrityError:
            exit_batch = KaspaExitBatch.objects.get(
                federation=self.federation,
                l2_chain_id=self.config.l2_chain_id,
                from_block=bundle.from_block,
                to_block=bundle.to_block,
            )
            self._validate_existing_exit_batch(exit_batch, evidence_hash)
            return exit_batch

    def _validate_existing_exit_batch(
        self,
        exit_batch: KaspaExitBatch,
        evidence_hash: str,
    ) -> None:
        expected = {
            "network": self.config.network,
            "evidence_hash": evidence_hash,
            "canonical_bridge_address": self.config.canonical_bridge_address,
            "canonical_bridge_script_public_key": (
                self.config.canonical_bridge_script_public_key
            ),
            "canonical_derivation_path": self.config.canonical_derivation_path,
            "threshold": self.federation.threshold,
            "xpub_fingerprint": self.federation.xpub_fingerprint,
        }
        for field, value in expected.items():
            if getattr(exit_batch, field) != value:
                raise KaspaExitProposalBuilderError(
                    f"existing exit batch conflicts on {field}"
                )

    def build_foundry_input(
        self,
        *,
        bundle_dir: str | Path,
        locking_utxos: list[dict[str, Any]],
        fee_sompi: int,
    ) -> dict[str, Any]:
        bundle = load_keb_bundle(bundle_dir)
        exit_requests = self._validate_and_extract_exits(bundle)
        exits = [
            {
                "message_id": request["message_id"],
                "recipient": request["recipient_address"],
                "amount_sompi": request["amount_sompi"],
                "amount_kas": _sompi_to_kas(request["amount_sompi"]),
            }
            for request in exit_requests
        ]
        normalized_utxos = [
            normalize_locking_utxo(
                utxo,
                bridge_address=self.config.canonical_bridge_address,
                bridge_script_public_key=self.config.canonical_bridge_script_public_key,
                derivation_path=self.config.canonical_derivation_path,
            )
            for utxo in locking_utxos
        ]
        total_input_sompi = sum(utxo["amount_sompi"] for utxo in normalized_utxos)
        exit_total_sompi = sum(exit["amount_sompi"] for exit in exits)
        change_sompi = total_input_sompi - exit_total_sompi - fee_sompi
        if change_sompi < 0:
            raise KaspaExitProposalBuilderError(
                "locking UTXOs do not cover exit outputs plus fee"
            )

        build_input = {
            "locking_utxos": normalized_utxos,
            "exits": exits,
            "fee_sompi": fee_sompi,
            "fee_kas": _sompi_to_kas(fee_sompi),
            "multisig": {
                "minimum_signatures": self.federation.threshold,
                "extended_public_keys": (
                    self.config.foundry_extended_public_keys or self.federation.xpubs
                ),
                "ecdsa": self.federation.ecdsa,
            },
        }
        funding_evidence = [
            utxo
            for utxo in locking_utxos
            if isinstance(utxo, dict)
            and ("selection" in utxo or "live_api_utxo" in utxo or "bridge_utxo" in utxo)
        ]
        if funding_evidence:
            build_input["kaspa_funding_evidence"] = {
                "selectedUtxos": funding_evidence,
                "normalizedLockingUtxos": normalized_utxos,
            }
        if change_sompi:
            build_input["change"] = {
                "derivation_path": self.config.canonical_derivation_path,
                "amount_sompi": change_sompi,
                "amount_kas": _sompi_to_kas(change_sompi),
                "address": self.config.canonical_bridge_address,
            }
        return build_input

    def required_input_sompi(self, *, bundle_dir: str | Path, fee_sompi: int) -> int:
        bundle = load_keb_bundle(bundle_dir)
        exit_requests = self._validate_and_extract_exits(bundle)
        return sum(request["amount_sompi"] for request in exit_requests) + fee_sompi

    def _validate_federation(self) -> None:
        if self.federation.network != self.config.network:
            raise KaspaExitProposalBuilderError("federation network mismatch")
        if self.federation.threshold <= 0:
            raise KaspaExitProposalBuilderError("federation threshold must be positive")
        if self.federation.threshold > len(self.federation.xpubs):
            raise KaspaExitProposalBuilderError(
                "federation threshold exceeds xpub count"
            )

    def _validate_and_extract_exits(self, bundle: "KebBundle") -> list[dict[str, Any]]:
        manifest_context = _expect_dict(bundle.manifest.get("context"), "manifest.context")
        _expect_equal(
            int(manifest_context["chainId"]),
            self.config.l2_chain_id,
            "manifest.context.chainId",
        )
        _expect_address_equal(
            manifest_context["kasExitBridge"],
            self.config.kas_exit_bridge,
            "manifest.context.kasExitBridge",
        )
        _expect_address_equal(
            manifest_context["mailbox"],
            self.config.mailbox,
            "manifest.context.mailbox",
        )
        _expect_address_equal(
            manifest_context["merkleTreeHook"],
            self.config.merkle_tree_hook,
            "manifest.context.merkleTreeHook",
        )

        _expect_equal(bundle.from_block, int(manifest_context["fromBlock"]), "fromBlock")
        _expect_equal(bundle.to_block, int(manifest_context["toBlock"]), "toBlock")
        _validate_checks(bundle.checks)
        _validate_contract_preverify(bundle.contract_preverify)

        exit_metadata = _expect_dict(bundle.exit_data.get("metadata"), "exitData.metadata")
        common = _expect_dict(exit_metadata.get("common"), "exitData.metadata.common")
        _expect_equal(int(common["chainId"]), self.config.l2_chain_id, "exitData chainId")
        _expect_address_equal(
            exit_metadata.get("kasExitBridge"),
            self.config.kas_exit_bridge,
            "exitData.kasExitBridge",
        )
        _expect_address_equal(
            exit_metadata.get("mailbox"),
            self.config.mailbox,
            "exitData.mailbox",
        )

        check_by_request_id = {
            int(item["requestId"]): item for item in bundle.checks.get("exits", [])
        }
        out: list[dict[str, Any]] = []
        for item in bundle.exit_data.get("exits", []):
            if item.get("status") != "success":
                continue
            request_id = int(item["requestId"])
            check_item = check_by_request_id.get(request_id, {})
            check_flags = check_item.get("checks") or {}
            failed_checks = [
                name for name, passed in check_flags.items() if passed is not True
            ]
            if failed_checks:
                raise KaspaExitProposalBuilderError(
                    f"exit request {request_id} failed checks: {failed_checks}"
                )

            decoded = _expect_dict(
                item.get("dispatchMessageDecoded"),
                f"exit {request_id}.dispatchMessageDecoded",
            )
            body = _expect_dict(decoded.get("body"), f"exit {request_id}.body")
            recipient = _expect_str(
                body.get("kasPayoutAddress"), f"exit {request_id}.kasPayoutAddress"
            )
            amount_sompi = int(body.get("unlockAmountSompi") or item["unlockAmountSompi"])
            _expect_equal(
                int(body.get("requestId", request_id)),
                request_id,
                f"exit {request_id}.requestId",
            )
            _expect_equal(
                amount_sompi,
                int(item["unlockAmountSompi"]),
                f"exit {request_id}.unlockAmountSompi",
            )
            out.append(
                {
                    "request_id": request_id,
                    "message_id": _expect_str(
                        item.get("messageId"), f"exit {request_id}.messageId"
                    ),
                    "block_number": int(item["blockNum"]),
                    "transaction_hash": _expect_str(
                        item.get("txHash"), f"exit {request_id}.txHash"
                    ),
                    "log_index": None,
                    "tree_index": (
                        int(item["insertedIntoTreeIndex"])
                        if item.get("insertedIntoTreeIndex") is not None
                        else None
                    ),
                    "recipient_address": recipient,
                    "amount_sompi": amount_sompi,
                    "burn_wei": str(item.get("burnWei") or ""),
                    "origin_burner_address": str(body.get("originBurner") or ""),
                    "dispatch_message": str(item.get("dispatchMessage") or ""),
                    "dispatch_decoded": decoded,
                    "raw": item,
                    "checks": check_flags,
                    "status": "success",
                }
            )

        if not out:
            raise KaspaExitProposalBuilderError("bundle contains no successful exits")
        totals = _expect_dict(exit_metadata.get("totals"), "exitData.metadata.totals")
        expected_total = int(totals["totalUnlockSompi"])
        actual_total = sum(item["amount_sompi"] for item in out)
        _expect_equal(actual_total, expected_total, "totalUnlockSompi")
        return out

    def _validate_unsigned_manifest(
        self,
        unsigned_manifest: dict[str, Any],
        verify_report: dict[str, Any],
    ) -> None:
        if unsigned_manifest.get("schema") != "igra.exit.unsigned.v1":
            raise KaspaExitProposalBuilderError("unsupported unsigned manifest schema")
        _expect_equal(
            unsigned_manifest.get("network"),
            self.config.network,
            "unsignedManifest.network",
        )
        protocol = _expect_dict(unsigned_manifest.get("protocol"), "unsigned.protocol")
        _expect_equal(protocol.get("payload_header"), "0x93", "payload header")
        prefix = _normalize_hex(protocol.get("tx_id_prefix", ""))
        _expect_equal(prefix, self.config.kaspa_tx_id_prefix, "tx id prefix")
        if verify_report and verify_report.get("ok") is not True:
            raise KaspaExitProposalBuilderError("unsigned verify report is not ok")
        if verify_report and verify_report.get("fully_signed") is True:
            raise KaspaExitProposalBuilderError(
                "proposal builder must submit unsigned PSTs only"
            )

    def _build_evidence(
        self,
        *,
        bundle: "KebBundle",
        exit_requests: list[dict[str, Any]],
        build_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        locking_utxos = []
        total_input_sompi = 0
        funding_evidence = {}
        if build_input:
            locking_utxos = list(build_input.get("locking_utxos") or [])
            total_input_sompi = sum(
                int(utxo.get("amount_sompi") or 0)
                for utxo in locking_utxos
                if isinstance(utxo, dict)
            )
            funding_evidence = dict(build_input.get("kaspa_funding_evidence") or {})
        return {
            "schemaVersion": 1,
            "kind": "kaspa-exit-proposal-evidence",
            "network": {
                "kaspa": self.config.network,
                "igraChainId": self.config.l2_chain_id,
                "igraRpcUrl": self.config.igra_rpc_url,
            },
            "window": {
                "fromBlock": bundle.from_block,
                "toBlock": bundle.to_block,
                "startCheckpointBlock": bundle.from_block - 1,
                "finalizedAtBlock": bundle.to_block,
                "deltaBlocks": bundle.to_block - bundle.from_block + 1,
                "l2ConfirmationBlocks": self.config.l2_confirmation_blocks,
            },
            "contracts": {
                "kasExitBridge": self.config.kas_exit_bridge,
                "mailbox": self.config.mailbox,
                "merkleTreeHook": self.config.merkle_tree_hook,
            },
            "bridge": {
                "address": self.config.canonical_bridge_address,
                "scriptPublicKey": self.config.canonical_bridge_script_public_key,
                "derivationPath": self.config.canonical_derivation_path,
                "threshold": self.federation.threshold,
                "ecdsa": self.federation.ecdsa,
                "xpubFingerprint": self.federation.xpub_fingerprint,
                "xpubs": self.federation.xpubs,
            },
            "exits": [
                {
                    "requestId": item["request_id"],
                    "messageId": item["message_id"],
                    "blockNumber": item["block_number"],
                    "transactionHash": item["transaction_hash"],
                    "treeIndex": item["tree_index"],
                    "recipient": item["recipient_address"],
                    "amountSompi": item["amount_sompi"],
                    "burnWei": item["burn_wei"],
                    "originBurner": item["origin_burner_address"],
                    "checks": item["checks"],
                }
                for item in exit_requests
            ],
            "kaspaFunding": {
                "source": "kaspa-node-rpc",
                "selectedUtxos": funding_evidence.get("selectedUtxos", locking_utxos),
                "normalizedLockingUtxos": locking_utxos,
                "selectedInputTotalSompi": total_input_sompi,
                "feeSompi": int(build_input.get("fee_sompi", 0)) if build_input else 0,
                "change": build_input.get("change") if build_input else None,
                "selectionPolicy": {
                    "description": (
                        "Select live custody UTXOs from configured Kaspa RPC, filter by "
                        "canonical bridge script/address and maturity, prefer non-coinbase "
                        "then older DAA score then larger amount, stop once exits plus fee "
                        "are covered."
                    ),
                    "coinbaseMaturityDaa": (
                        self.config.kaspa_utxos.coinbase_maturity_daa
                        if self.config.kaspa_utxos
                        else None
                    ),
                    "minConfirmationsDaa": (
                        self.config.kaspa_utxos.min_confirmations_daa
                        if self.config.kaspa_utxos
                        else None
                    ),
                    "minAmountSompi": (
                        self.config.kaspa_utxos.min_amount_sompi
                        if self.config.kaspa_utxos
                        else None
                    ),
                    "maxInputs": (
                        self.config.kaspa_utxos.max_inputs
                        if self.config.kaspa_utxos
                        else None
                    ),
                },
            },
            "bundle": {
                "manifest": bundle.manifest,
                "exitData": bundle.exit_data,
                "checks": bundle.checks,
                "treeData": bundle.tree_data,
                "checkpointEnd": bundle.checkpoint_end,
                "contractPreverify": bundle.contract_preverify,
                "rawJsonArtifacts": bundle.raw_json_artifacts,
            },
            "verificationCommands": [
                "cast igra verify-bundle-integral --manifest <bundle>/manifest.json",
                "cast igra verify-exit --manifest <proposal-origin> --hex <proposal>",
                "kaspa-pst inspect",
            ],
        }

    def _checks_summary(self, bundle: "KebBundle") -> dict[str, Any]:
        return {
            "globalErrors": bundle.checks.get("globalErrors"),
            "exit": (bundle.checks.get("metadata") or {}).get("exit"),
            "tree": (bundle.checks.get("metadata") or {}).get("tree"),
        }


@dataclass(frozen=True)
class KebBundle:
    bundle_dir: Path
    manifest: dict[str, Any]
    exit_data: dict[str, Any]
    checks: dict[str, Any]
    tree_data: dict[str, Any]
    checkpoint_end: dict[str, Any]
    contract_preverify: dict[str, Any]
    raw_json_artifacts: dict[str, Any]

    @property
    def from_block(self) -> int:
        return int(self.manifest["context"]["fromBlock"])

    @property
    def to_block(self) -> int:
        return int(self.manifest["context"]["toBlock"])


def load_keb_bundle(bundle_dir: str | Path) -> KebBundle:
    bundle_dir = Path(bundle_dir)
    if not bundle_dir.is_dir():
        raise KaspaExitProposalBuilderError(f"bundle directory not found: {bundle_dir}")

    raw_json_artifacts: dict[str, Any] = {}
    for path in sorted(bundle_dir.rglob("*.json")):
        relative = path.relative_to(bundle_dir).as_posix()
        raw_json_artifacts[relative] = _load_json(path)

    def artifact(relative_path: str) -> dict[str, Any]:
        try:
            value = raw_json_artifacts[relative_path]
        except KeyError as exc:
            raise KaspaExitProposalBuilderError(
                f"bundle is missing {relative_path}"
            ) from exc
        return _expect_dict(value, relative_path)

    return KebBundle(
        bundle_dir=bundle_dir,
        manifest=artifact("manifest.json"),
        exit_data=artifact("derived/exit.data.json"),
        checks=artifact("derived/checks.json"),
        tree_data=artifact("derived/tree.data.json"),
        checkpoint_end=artifact("derived/checkpoint.end.json"),
        contract_preverify=artifact("derived/contract.preverify.json"),
        raw_json_artifacts=raw_json_artifacts,
    )


def normalize_locking_utxo(
    value: dict[str, Any],
    *,
    bridge_address: str,
    bridge_script_public_key: str,
    derivation_path: str,
) -> dict[str, Any]:
    if "transaction_id" in value:
        utxo = dict(value)
    else:
        bridge_utxo = _expect_dict(value.get("bridge_utxo"), "bridge_utxo")
        live_utxo = value.get("live_api_utxo") or {}
        live_entry = live_utxo.get("utxoEntry") or {}
        utxo = {
            "transaction_id": bridge_utxo["transaction_id"],
            "index": bridge_utxo.get("output_index", bridge_utxo.get("index")),
            "amount_sompi": int(
                bridge_utxo.get("amount_sompi")
                or live_entry.get("amount")
            ),
            "address": live_utxo.get("address") or bridge_address,
            "script_public_key": {
                "version": 0,
                "script": _normalize_hex(bridge_utxo["script_public_key"]),
            },
            "derivation_path": derivation_path,
        }

    script = _normalize_hex(utxo["script_public_key"]["script"])
    if script != _normalize_hex(bridge_script_public_key):
        raise KaspaExitProposalBuilderError("locking UTXO script does not match bridge")
    if utxo.get("address") and utxo["address"] != bridge_address:
        raise KaspaExitProposalBuilderError("locking UTXO address does not match bridge")

    utxo["amount_sompi"] = int(utxo["amount_sompi"])
    utxo["index"] = int(utxo["index"])
    utxo["script_public_key"]["script"] = script
    utxo["derivation_path"] = derivation_path
    utxo.setdefault("amount_kas", _sompi_to_kas(utxo["amount_sompi"]))
    utxo.setdefault("address", bridge_address)
    return utxo


def strip_builder_only_build_input_fields(build_input: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in build_input.items()
        if key not in {"kaspa_funding_evidence"}
    }


def normalize_pst_xpub_versions(
    unsigned_bundle_hex: str,
    network: str,
) -> tuple[str, dict[str, Any] | None]:
    target_version = _KASPA_PUBLIC_XPUB_VERSIONS.get(network)
    if target_version is None:
        return unsigned_bundle_hex, None

    try:
        wallet_bytes = bytes.fromhex(unsigned_bundle_hex)
    except ValueError as exc:
        raise KaspaExitProposalBuilderError("unsigned PST hex is invalid") from exc

    replacements: list[dict[str, Any]] = []
    normalized = wallet_bytes
    candidates = {match.group(1) for match in _BASE58_XPUB_RE.finditer(wallet_bytes)}
    for candidate in sorted(candidates):
        try:
            decoded = _base58check_decode(candidate.decode())
        except ValueError:
            continue
        source_version = decoded[:4]
        if source_version not in _KASPA_PUBLIC_XPUB_VERSIONS.values():
            continue
        if source_version == target_version:
            continue

        rewritten = _base58check_encode(target_version + decoded[4:]).encode()
        if len(rewritten) != len(candidate):
            raise KaspaExitProposalBuilderError(
                "xpub network rewrite changed encoded length"
            )
        count = normalized.count(candidate)
        if count <= 0:
            continue
        normalized = normalized.replace(candidate, rewritten)
        replacements.append(
            {
                "from": candidate.decode(),
                "to": rewritten.decode(),
                "fromVersion": source_version.hex(),
                "toVersion": target_version.hex(),
                "count": count,
            }
        )

    if not replacements:
        return unsigned_bundle_hex, None

    normalized_hex = normalized.hex()
    return normalized_hex, {
        "applied": True,
        "targetNetwork": network,
        "targetXpubVersion": target_version.hex(),
        "originalBundleHex": unsigned_bundle_hex,
        "originalBundleHexSha256": hashlib.sha256(
            unsigned_bundle_hex.encode()
        ).hexdigest(),
        "normalizedBundleHexSha256": hashlib.sha256(
            normalized_hex.encode()
        ).hexdigest(),
        "replacements": replacements,
        "reason": (
            "Foundry and kaspawallet can encode the same BIP32 public key with "
            "different Kaspa network xpub versions on devnet/test networks. "
            "Only PST signer metadata is rewritten; transaction inputs, outputs, "
            "scripts, and payload are unchanged."
        ),
    }


def _base58check_decode(value: str) -> bytes:
    number = 0
    for char in value:
        try:
            digit = _BASE58_INDEX[char]
        except KeyError as exc:
            raise ValueError("invalid base58 character") from exc
        number = number * 58 + digit

    payload = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    payload = b"\x00" * (len(value) - len(value.lstrip("1"))) + payload
    if len(payload) != 82:
        raise ValueError("invalid extended key payload length")
    checksum = hashlib.sha256(hashlib.sha256(payload[:-4]).digest()).digest()[:4]
    if checksum != payload[-4:]:
        raise ValueError("invalid base58 checksum")
    return payload[:-4]


def _base58check_encode(payload: bytes) -> str:
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    raw = payload + checksum
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, index = divmod(number, 58)
        encoded = _BASE58_ALPHABET[index] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + encoded


def _validate_checks(checks: dict[str, Any]) -> None:
    global_errors = checks.get("globalErrors") or {}
    for key, value in global_errors.items():
        if value:
            raise KaspaExitProposalBuilderError(f"bundle global {key} errors: {value}")

    metadata = checks.get("metadata") or {}
    exit_meta = ((metadata.get("exit") or {}).get("totals") or {})
    tree_meta = metadata.get("tree") or {}
    tree_totals = tree_meta.get("totals") or {}
    root_replay = (tree_meta.get("rootReplay") or {})
    if exit_meta.get("anyCheckFailed", 0) != 0:
        raise KaspaExitProposalBuilderError("exit checks report failures")
    if tree_totals.get("anyCheckFailed", 0) != 0:
        raise KaspaExitProposalBuilderError("tree checks report failures")
    if root_replay.get("enabled") and root_replay.get("match") is not True:
        raise KaspaExitProposalBuilderError("tree root replay did not match")


def _validate_contract_preverify(contract_preverify: dict[str, Any]) -> None:
    for contract in contract_preverify.get("contracts", []):
        if contract.get("allMatch") is not True:
            raise KaspaExitProposalBuilderError(
                f"contract preverify failed for {contract.get('id')}"
            )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except OSError as exc:
        raise KaspaExitProposalBuilderError(f"failed to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise KaspaExitProposalBuilderError(f"invalid JSON in {path}: {exc}") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _expect_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KaspaExitProposalBuilderError(f"{field} must be an object")
    return value


def _expect_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise KaspaExitProposalBuilderError(f"{field} must be a non-empty string")
    return value


def _expect_str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise KaspaExitProposalBuilderError(f"{field} must be a non-empty array")
    for index, item in enumerate(value):
        _expect_str(item, f"{field}[{index}]")
    return value


def _expect_equal(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise KaspaExitProposalBuilderError(
            f"{field} mismatch: expected {expected!r}, actual {actual!r}"
        )


def _expect_address_equal(actual: Any, expected: str, field: str) -> None:
    actual_str = _expect_str(actual, field)
    if actual_str.lower() != expected.lower():
        raise KaspaExitProposalBuilderError(
            f"{field} mismatch: expected {expected}, actual {actual_str}"
        )


def _normalize_hex(value: str) -> str:
    return value.strip().removeprefix("0x").lower()


def _sompi_to_kas(value: int) -> str:
    whole = value // 100_000_000
    fraction = value % 100_000_000
    return f"{whole}.{fraction:08d}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
