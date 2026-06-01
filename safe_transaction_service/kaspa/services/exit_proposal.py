# SPDX-License-Identifier: FSL-1.1-MIT
import hashlib
import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
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


@dataclass(frozen=True)
class KaspaExitProposalBuildResult:
    exit_batch: KaspaExitBatch
    proposal: KaspaTxProposal | None
    evidence_hash: str
    unsigned_manifest: dict[str, Any]
    unsigned_bundle_hex: str


def load_builder_config(path: str | Path) -> KaspaExitProposalBuilderConfig:
    data = _load_json(Path(path))
    contracts = _expect_dict(data.get("contracts"), "contracts")
    bridge = _expect_dict(data.get("bridge"), "bridge")
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
    )


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
            input_path.write_text(json.dumps(build_input, indent=2) + "\n")

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

        evidence = self._build_evidence(
            bundle=bundle,
            exit_requests=exit_requests,
            build_input=build_input,
            unsigned_manifest=unsigned_manifest,
            unsigned_verify_report=unsigned_verify_report,
        )
        evidence_hash = canonical_json_hash(evidence)
        artifact_hashes = dict(bundle.manifest.get("artifactChecksums") or {})
        artifact_hashes["unsignedManifestSha256"] = _sha256_json(unsigned_manifest)
        artifact_hashes["unsignedBundleHexSha256"] = hashlib.sha256(
            unsigned_bundle_hex.encode()
        ).hexdigest()
        if build_input:
            artifact_hashes["buildInputSha256"] = _sha256_json(build_input)

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
        except IntegrityError as exc:
            raise KaspaExitProposalBuilderError(
                "Kaspa exit batch already exists for this federation/window"
            ) from exc

        proposal = None
        if submit_proposal:
            serializer = KaspaTxProposalCreateSerializer(
                data={
                    "unsignedBundleHex": unsigned_bundle_hex,
                    "exitBatch": str(exit_batch.pk),
                    "proposedBy": self.config.proposed_by,
                    "origin": {
                        "kind": "igra-l2-exit",
                        "builder": "safe-transaction-service",
                        "evidenceHash": evidence_hash,
                    },
                },
                context={"federation": self.federation},
            )
            try:
                serializer.is_valid(raise_exception=True)
                proposal = serializer.save()
            except ValidationError as exc:
                exit_batch.status = KaspaExitBatchStatus.FAILED
                exit_batch.save(update_fields=["status", "modified"])
                raise KaspaExitProposalBuilderError(str(exc.detail)) from exc

        return KaspaExitProposalBuildResult(
            exit_batch=exit_batch,
            proposal=proposal,
            evidence_hash=evidence_hash,
            unsigned_manifest=unsigned_manifest,
            unsigned_bundle_hex=unsigned_bundle_hex,
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
                "extended_public_keys": self.federation.xpubs,
                "ecdsa": self.federation.ecdsa,
            },
        }
        if change_sompi:
            build_input["change"] = {
                "derivation_path": self.config.canonical_derivation_path,
                "amount_sompi": change_sompi,
                "amount_kas": _sompi_to_kas(change_sompi),
                "address": self.config.canonical_bridge_address,
            }
        return build_input

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
        build_input: dict[str, Any] | None,
        unsigned_manifest: dict[str, Any],
        unsigned_verify_report: dict[str, Any],
    ) -> dict[str, Any]:
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
            "bundle": {
                "manifest": bundle.manifest,
                "exitData": bundle.exit_data,
                "checks": bundle.checks,
                "treeData": bundle.tree_data,
                "checkpointEnd": bundle.checkpoint_end,
                "contractPreverify": bundle.contract_preverify,
                "rawJsonArtifacts": bundle.raw_json_artifacts,
            },
            "kaspaTransaction": {
                "buildInput": build_input,
                "unsignedManifest": unsigned_manifest,
                "unsignedVerify": unsigned_verify_report,
            },
            "verificationCommands": [
                "cast igra verify-bundle-integral --manifest <bundle>/manifest.json",
                "cast igra verify-exit --manifest <unsigned.json> --hex <unsigned.hex>",
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
