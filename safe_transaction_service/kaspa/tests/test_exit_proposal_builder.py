# SPDX-License-Identifier: FSL-1.1-MIT
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from safe_transaction_service.kaspa.models import (
    KaspaExitBatchStatus,
    KaspaExitRequest,
    KaspaFederation,
    KaspaTxProposal,
)
from safe_transaction_service.kaspa.serializers import canonical_json_hash
from safe_transaction_service.kaspa.services.exit_proposal import (
    KaspaExitProposalBuilder,
    KaspaExitProposalBuilderConfig,
    KaspaExitProposalBuilderError,
    normalize_pst_xpub_versions,
)


class TestKaspaExitProposalBuilder(TestCase):
    def setUp(self):
        self.config = KaspaExitProposalBuilderConfig(
            network="mainnet",
            l2_chain_id=38833,
            igra_rpc_url="https://rpc.igralabs.com:8545",
            kas_exit_bridge="0x4bb88C213d3eD9dc4bae694f1bc1bF745903b2d0",
            mailbox="0x3a867fCfFeC2B790970eeBDC9023E75B0a172aa7",
            merkle_tree_hook="0x75719C858e0c73e07128F95B2C466d142490e933",
            canonical_bridge_address="kaspa:bridge",
            canonical_bridge_script_public_key="aa20",
            kaspa_tx_id_prefix="97b1",
        )
        xpubs = ["kpub-a", "kpub-b", "kpub-c"]
        self.federation = KaspaFederation.objects.create(
            name="Bridge",
            network="mainnet",
            xpubs=xpubs,
            xpub_fingerprint=canonical_json_hash(xpubs),
            threshold=2,
        )

    @mock.patch("safe_transaction_service.kaspa.serializers.get_pst_client")
    def test_build_from_verified_bundle_creates_batch_and_proposal(
        self, get_pst_client_mock
    ):
        pst_client = get_pst_client_mock.return_value
        pst_client.inspect.return_value = {
            "proposalHash": "a" * 64,
            "xpubFingerprint": self.federation.xpub_fingerprint,
            "txIds": ["97b1tx"],
            "inputOutpoints": [{"txId": "prev", "index": 0, "amountSompi": 300}],
            "outputs": [{"address": "kaspa:recipient", "amountSompi": 200}],
            "feeSompi": 100,
            "mass": 1200,
            "signaturesRequired": 2,
            "signaturesCollected": 0,
            "ready": False,
        }

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = self.write_bundle(Path(tmp))
            builder = KaspaExitProposalBuilder(
                config=self.config, federation=self.federation
            )
            result = builder.build_from_artifacts(
                bundle_dir=bundle_dir,
                unsigned_manifest=self.unsigned_manifest(),
                unsigned_bundle_hex="aa",
                unsigned_verify_report={
                    "ok": True,
                    "signed_inputs": 0,
                    "fully_signed": False,
                },
                build_input={"locking_utxos": []},
            )

        self.assertEqual(result.exit_batch.status, KaspaExitBatchStatus.PROPOSED)
        self.assertEqual(result.proposal.proposal_hash, "a" * 64)
        self.assertEqual(KaspaTxProposal.objects.count(), 1)
        exit_request = KaspaExitRequest.objects.get()
        self.assertEqual(exit_request.request_id, 1)
        self.assertEqual(exit_request.recipient_address, "kaspa:recipient")
        self.assertEqual(exit_request.amount_sompi, 200)
        self.assertEqual(
            result.exit_batch.evidence["bundle"]["manifest"]["context"]["chainId"],
            38833,
        )

    def test_rejects_wrong_contract(self):
        bad_config = KaspaExitProposalBuilderConfig(
            **{**self.config.__dict__, "kas_exit_bridge": "0x" + "1" * 40}
        )
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = self.write_bundle(Path(tmp))
            builder = KaspaExitProposalBuilder(
                config=bad_config, federation=self.federation
            )
            with self.assertRaises(KaspaExitProposalBuilderError):
                builder.build_from_artifacts(
                    bundle_dir=bundle_dir,
                    unsigned_manifest=self.unsigned_manifest(),
                    unsigned_bundle_hex="aa",
                    unsigned_verify_report={
                        "ok": True,
                        "signed_inputs": 0,
                        "fully_signed": False,
                    },
                    submit_proposal=False,
                )

    def test_build_foundry_input_from_live_utxo_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = self.write_bundle(Path(tmp))
            builder = KaspaExitProposalBuilder(
                config=self.config, federation=self.federation
            )
            build_input = builder.build_foundry_input(
                bundle_dir=bundle_dir,
                locking_utxos=[
                    {
                        "bridge_utxo": {
                            "transaction_id": "97b1" + "0" * 60,
                            "output_index": 0,
                            "amount_sompi": 500,
                            "script_public_key": "0xaa20",
                        },
                        "live_api_utxo": {"address": "kaspa:bridge"},
                    }
                ],
                fee_sompi=100,
            )

        self.assertEqual(build_input["exits"][0]["message_id"], "0x" + "b" * 64)
        self.assertEqual(build_input["change"]["amount_sompi"], 200)
        self.assertEqual(build_input["multisig"]["minimum_signatures"], 2)
        self.assertEqual(
            build_input["locking_utxos"][0]["script_public_key"]["script"], "aa20"
        )

    def test_build_foundry_input_can_use_foundry_extended_public_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = self.write_bundle(Path(tmp))
            config = KaspaExitProposalBuilderConfig(
                **{
                    **self.config.__dict__,
                    "foundry_extended_public_keys": ["kpub-root-a", "kpub-root-b"],
                }
            )
            builder = KaspaExitProposalBuilder(config=config, federation=self.federation)

            build_input = builder.build_foundry_input(
                bundle_dir=bundle_dir,
                locking_utxos=[
                    {
                        "bridge_utxo": {
                            "transaction_id": "97b1" + "0" * 60,
                            "output_index": 0,
                            "amount_sompi": 500,
                            "script_public_key": "0xaa20",
                        },
                        "live_api_utxo": {"address": "kaspa:bridge"},
                    }
                ],
                fee_sompi=100,
            )

        self.assertEqual(
            build_input["multisig"]["extended_public_keys"],
            ["kpub-root-a", "kpub-root-b"],
        )

    def test_normalize_pst_xpub_versions_rewrites_to_devnet(self):
        ktub_child = (
            "ktub28c2yq6MoXoAQGMBAXquWomkg6VbY9caC3BCGRaqtGZQuUcypSPBcfyQxFi8"
            "W6FdDsA8xRjr2vAVRdQUz72vgNEeD2AgJ9YXh41PtnvM1b7"
        )
        kdub_child = (
            "kdub5CSz6Y1Rm7cpRJWHXLAz4YR2UtrNbfSxvtGEwMZLiX3bnoG75VHqdGK9M6"
            "YuZYWJg2oRkeBg4GeaaRJ6bFopx57TZk8ywpeoRj32xukXynF"
        )
        bundle_hex = (b"\x08" + ktub_child.encode() + b"\x12").hex()

        normalized_hex, report = normalize_pst_xpub_versions(bundle_hex, "devnet")

        self.assertIn(kdub_child.encode().hex(), normalized_hex)
        self.assertNotIn(ktub_child.encode().hex(), normalized_hex)
        self.assertEqual(report["targetNetwork"], "devnet")
        self.assertEqual(report["replacements"][0]["from"], ktub_child)
        self.assertEqual(report["replacements"][0]["to"], kdub_child)

    def write_bundle(self, root: Path) -> Path:
        bundle_dir = root / "keb-from-100-to-199-test.bundle"
        (bundle_dir / "derived").mkdir(parents=True)
        manifest = {
            "schemaVersion": 1,
            "kind": "kas-exit-bridge-pass-a-bundle",
            "context": {
                "rpcUrl": self.config.igra_rpc_url,
                "chainId": 38833,
                "fromBlock": 100,
                "toBlock": 199,
                "kasExitBridge": self.config.kas_exit_bridge,
                "mailbox": self.config.mailbox,
                "merkleTreeHook": self.config.merkle_tree_hook,
            },
            "artifactChecksums": {},
        }
        exit_data = {
            "metadata": {
                "common": {"chainId": 38833, "fromBlock": 100, "toBlock": 199},
                "kasExitBridge": self.config.kas_exit_bridge,
                "mailbox": self.config.mailbox,
                "totals": {"totalUnlockSompi": "200"},
            },
            "exits": [
                {
                    "status": "success",
                    "requestId": 1,
                    "blockNum": 120,
                    "txHash": "0x" + "a" * 64,
                    "unlockAmountSompi": "200",
                    "burnWei": "2000000000000",
                    "messageId": "0x" + "b" * 64,
                    "dispatchMessage": "0xdeadbeef",
                    "dispatchMessageDecoded": {
                        "body": {
                            "requestId": 1,
                            "unlockAmountSompi": "200",
                            "originBurner": "0x" + "c" * 40,
                            "kasPayoutAddress": "kaspa:recipient",
                        }
                    },
                    "insertedIntoTreeIndex": 9,
                }
            ],
        }
        checks = {
            "metadata": {
                "exit": {"totals": {"anyCheckFailed": 0}},
                "tree": {
                    "rootReplay": {"enabled": True, "match": True},
                    "totals": {"anyCheckFailed": 0},
                },
            },
            "globalErrors": {"exit": [], "tree": []},
            "exits": [{"requestId": 1, "checks": {"allLocalChecks": True}}],
        }
        self.write_json(bundle_dir / "manifest.json", manifest)
        self.write_json(bundle_dir / "derived" / "exit.data.json", exit_data)
        self.write_json(bundle_dir / "derived" / "checks.json", checks)
        self.write_json(bundle_dir / "derived" / "tree.data.json", {})
        self.write_json(bundle_dir / "derived" / "checkpoint.end.json", {})
        self.write_json(
            bundle_dir / "derived" / "contract.preverify.json",
            {"contracts": [{"id": "kasExitBridge", "allMatch": True}]},
        )
        return bundle_dir

    def unsigned_manifest(self):
        return {
            "schema": "igra.exit.unsigned.v1",
            "network": "mainnet",
            "protocol": {
                "payload_header": "0x93",
                "tx_id_prefix": "0x97b1",
                "kaspa_tx_id": "97b1tx",
            },
        }

    def write_json(self, path: Path, value):
        path.write_text(json.dumps(value))
