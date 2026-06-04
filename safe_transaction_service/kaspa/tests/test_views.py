# SPDX-License-Identifier: FSL-1.1-MIT
from unittest import mock
from unittest.mock import MagicMock

from django.urls import reverse

from rest_framework import status
from rest_framework.test import APITestCase

from safe_transaction_service.kaspa.models import (
    KaspaBroadcastAttempt,
    KaspaExitBatch,
    KaspaExitBatchStatus,
    KaspaExitRequest,
    KaspaFederation,
    KaspaTxProposal,
    KaspaTxProposalStatus,
    KaspaTxSignature,
)
from safe_transaction_service.kaspa.serializers import canonical_json_hash
from safe_transaction_service.kaspa.services.pst import KaspaPstError


class TestKaspaViews(APITestCase):
    xpubs = ["kpub-c", "kpub-a", "kpub-b"]

    def create_federation(self) -> KaspaFederation:
        response = self.client.post(
            reverse("v1:kaspa:federations"),
            data={
                "name": "Treasury",
                "network": "mainnet",
                "threshold": 2,
                "xpubs": self.xpubs,
                "participants": [
                    {"name": "Alice", "xpub": "kpub-a"},
                    {"name": "Bob", "xpub": "kpub-b"},
                    {"name": "Carol", "xpub": "kpub-c"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return KaspaFederation.objects.get(pk=response.json()["id"])

    def create_exit_batch(self, federation: KaspaFederation) -> KaspaExitBatch:
        exit_batch = KaspaExitBatch.objects.create(
            federation=federation,
            network=federation.network,
            l2_chain_id=38833,
            from_block=7344000,
            to_block=7430399,
            finalized_at_block=7430399,
            status=KaspaExitBatchStatus.VERIFIED,
            evidence_hash="e" * 64,
            total_exits=1,
            total_amount_sompi=200,
            canonical_bridge_address="kaspa:bridge",
            canonical_bridge_script_public_key="aa20",
            canonical_derivation_path="m/0/0/1",
            threshold=federation.threshold,
            xpub_fingerprint=federation.xpub_fingerprint,
            checks={"ok": True},
            artifact_hashes={"exitData": "d" * 64},
            evidence={
                "schemaVersion": 1,
                "kind": "kaspa-exit-proposal-evidence",
                "window": {"fromBlock": 7344000, "toBlock": 7430399},
            },
        )
        KaspaExitRequest.objects.create(
            batch=exit_batch,
            request_id=35,
            message_id="0x" + "1" * 64,
            block_number=7344010,
            transaction_hash="0x" + "2" * 64,
            log_index=7,
            tree_index=128,
            recipient_address="kaspa:recipient",
            amount_sompi=200,
            burn_wei="2000000000000",
            origin_burner_address="0x" + "3" * 40,
            dispatch_message="0xdeadbeef",
            dispatch_decoded={"body": {"kasPayoutAddress": "kaspa:recipient"}},
            checks={"ok": True},
        )
        return exit_batch

    def test_create_federation(self):
        federation = self.create_federation()

        self.assertEqual(federation.xpubs, ["kpub-a", "kpub-b", "kpub-c"])
        self.assertEqual(federation.xpub_fingerprint, canonical_json_hash(federation.xpubs))
        self.assertEqual(federation.participants.count(), 3)
        self.assertEqual(
            list(federation.participants.values_list("name", "cosigner_index")),
            [("Alice", 0), ("Bob", 1), ("Carol", 2)],
        )

    @mock.patch("safe_transaction_service.kaspa.serializers.get_pst_client")
    def test_propose_sign_and_broadcast(self, get_pst_client_mock: MagicMock):
        federation = self.create_federation()
        pst_client = get_pst_client_mock.return_value
        pst_client.inspect.return_value = {
            "proposalHash": "a" * 64,
            "xpubFingerprint": federation.xpub_fingerprint,
            "txIds": ["tx-unsigned"],
            "inputOutpoints": [{"txId": "prev", "index": 0, "amountSompi": 300}],
            "outputs": [{"address": "kaspa:qtest", "amountSompi": 200}],
            "feeSompi": 100,
            "mass": 1200,
            "signaturesRequired": 2,
            "signaturesCollected": 0,
            "ready": False,
        }

        response = self.client.post(
            reverse("v1:kaspa:federation-transactions", args=(federation.pk,)),
            data={
                "unsignedBundleHex": "aa",
                "proposedBy": "Alice",
                "origin": {"note": "Treasury payout"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        proposal = KaspaTxProposal.objects.get(proposal_hash="a" * 64)
        self.assertEqual(proposal.status, KaspaTxProposalStatus.PENDING)
        self.assertEqual(proposal.merged_bundle_hex, "aa")
        self.assertEqual(proposal.signatures_required, 2)
        self.assertEqual(proposal.signatures_collected, 0)

        pst_client.merge.return_value = {
            "mergedBundleHex": "aabb",
            "addedSignatures": [
                {"tx": 0, "input": 0, "pubKey": "pub-a", "signature": "sig-a"},
                {"tx": 0, "input": 0, "pubKey": "pub-b", "signature": "sig-b"},
            ],
            "signaturesCollected": 2,
            "ready": True,
            "txIds": ["tx-final"],
        }

        response = self.client.post(
            reverse("v1:kaspa:transaction-signatures", args=(proposal.proposal_hash,)),
            data={"signedBundleHex": "bb", "signer": "Alice"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, KaspaTxProposalStatus.READY)
        self.assertEqual(proposal.merged_bundle_hex, "aabb")
        self.assertEqual(proposal.signatures_collected, 2)
        self.assertEqual(KaspaTxSignature.objects.count(), 1)

        pst_client.broadcast.return_value = {"txIds": ["tx-final"]}
        response = self.client.post(
            reverse("v1:kaspa:transaction-broadcast", args=(proposal.proposal_hash,)),
            data={},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, KaspaTxProposalStatus.BROADCASTED)
        self.assertEqual(proposal.broadcast_tx_ids, ["tx-final"])
        self.assertEqual(KaspaBroadcastAttempt.objects.filter(success=True).count(), 1)

    @mock.patch("safe_transaction_service.kaspa.serializers.get_pst_client")
    def test_create_proposal_infers_federation_from_payload(
        self, get_pst_client_mock: MagicMock
    ):
        xpubs = ["kpub-a", "kpub-b", "kpub-c"]
        fingerprint = canonical_json_hash(xpubs)
        pst_client = get_pst_client_mock.return_value
        pst_client.inspect.side_effect = [
            {
                "proposalHash": "d" * 64,
                "xpubFingerprint": fingerprint,
                "txIds": ["tx-unsigned-1"],
                "inputOutpoints": [{"txId": "prev", "index": 0, "amountSompi": 300}],
                "outputs": [{"address": "kaspa:qtest", "amountSompi": 200}],
                "feeSompi": 100,
                "mass": 1200,
                "signaturesRequired": 2,
                "signaturesCollected": 0,
                "ready": False,
            },
            {
                "proposalHash": "e" * 64,
                "xpubFingerprint": fingerprint,
                "txIds": ["tx-unsigned-2"],
                "inputOutpoints": [{"txId": "prev2", "index": 0, "amountSompi": 400}],
                "outputs": [{"address": "kaspa:qtest2", "amountSompi": 300}],
                "feeSompi": 100,
                "mass": 1200,
                "signaturesRequired": 2,
                "signaturesCollected": 0,
                "ready": False,
            },
        ]
        payload = {
            "federation": {
                "name": "Open federation",
                "network": "mainnet",
                "threshold": 2,
                "xpubs": self.xpubs,
                "participants": [
                    {"name": "Alice", "xpub": "kpub-a"},
                    {"name": "Bob", "xpub": "kpub-b"},
                    {"name": "Carol", "xpub": "kpub-c"},
                ],
            },
            "unsignedBundleHex": "aa",
            "proposedBy": "open-builder",
            "origin": {"note": "self-service"},
        }

        response = self.client.post(
            reverse("v1:kaspa:transactions"),
            data=payload,
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        federation = KaspaFederation.objects.get()
        self.assertEqual(federation.xpub_fingerprint, fingerprint)
        self.assertEqual(federation.participants.count(), 3)
        self.assertEqual(response.json()["federation"], str(federation.pk))

        payload["unsignedBundleHex"] = "bb"
        response = self.client.post(
            reverse("v1:kaspa:transactions"),
            data=payload,
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(KaspaFederation.objects.count(), 1)
        self.assertEqual(KaspaTxProposal.objects.count(), 2)

    def test_create_exit_batch_infers_federation_from_evidence(self):
        evidence = {
            "schemaVersion": 1,
            "kind": "kaspa-exit-proposal-evidence",
            "network": {"kaspa": "mainnet", "igraChainId": 38833},
            "window": {"fromBlock": 7344000, "toBlock": 7430399},
            "bridge": {
                "address": "kaspa:bridge",
                "scriptPublicKey": "aa20",
                "derivationPath": "m/0/0/1",
                "threshold": 2,
                "ecdsa": False,
                "xpubFingerprint": canonical_json_hash(
                    ["kpub-a", "kpub-b", "kpub-c"]
                ),
                "xpubs": ["kpub-c", "kpub-a", "kpub-b"],
            },
            "exits": [
                {
                    "requestId": 35,
                    "messageId": "0x" + "1" * 64,
                    "recipient": "kaspa:recipient",
                    "amountSompi": 200,
                }
            ],
            "bundle": {
                "checks": {
                    "globalErrors": [],
                    "metadata": {
                        "exit": {"totals": {"failed": 0}},
                        "tree": {"totals": {"failed": 0}},
                    },
                }
            },
        }

        response = self.client.post(
            reverse("v1:kaspa:exit-batches"),
            data={
                "network": "mainnet",
                "l2ChainId": 38833,
                "fromBlock": 7344000,
                "toBlock": 7430399,
                "finalizedAtBlock": 7430399,
                "evidence": evidence,
                "exitRequests": [
                    {
                        "requestId": 35,
                        "messageId": "0x" + "1" * 64,
                        "blockNumber": 7344010,
                        "transactionHash": "0x" + "2" * 64,
                        "logIndex": 7,
                        "treeIndex": 128,
                        "recipientAddress": "kaspa:recipient",
                        "amountSompi": 200,
                        "burnWei": "2000000000000",
                        "originBurnerAddress": "0x" + "3" * 40,
                        "dispatchMessage": "0xdeadbeef",
                        "dispatchDecoded": {
                            "body": {"kasPayoutAddress": "kaspa:recipient"}
                        },
                        "checks": {"ok": True},
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        exit_batch = KaspaExitBatch.objects.get()
        federation = KaspaFederation.objects.get()
        self.assertEqual(exit_batch.federation_id, federation.id)
        self.assertEqual(exit_batch.evidence_hash, canonical_json_hash(evidence))
        self.assertEqual(exit_batch.exit_requests.count(), 1)
        self.assertEqual(response.json()["evidenceHash"], exit_batch.evidence_hash)

        response = self.client.post(
            reverse("v1:kaspa:exit-batches"),
            data={
                "network": "mainnet",
                "l2ChainId": 38833,
                "fromBlock": 7344000,
                "toBlock": 7430399,
                "finalizedAtBlock": 7430399,
                "evidence": evidence,
                "exitRequests": [],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(KaspaExitBatch.objects.count(), 1)

    def test_same_window_accepts_distinct_evidence_batches(self):
        federation = self.create_federation()
        first = self.create_exit_batch(federation)
        second_evidence = {
            "schemaVersion": 1,
            "kind": "kaspa-exit-proposal-evidence",
            "network": {"kaspa": "mainnet", "igraChainId": 38833},
            "window": {"fromBlock": 7344000, "toBlock": 7430399},
            "bridge": {
                "address": "kaspa:bridge",
                "scriptPublicKey": "aa20",
                "derivationPath": "m/0/0/1",
                "threshold": 2,
                "ecdsa": False,
                "xpubFingerprint": federation.xpub_fingerprint,
                "xpubs": self.xpubs,
            },
            "exits": [],
            "candidate": "different",
        }

        response = self.client.post(
            reverse("v1:kaspa:exit-batches"),
            data={
                "federation": {
                    "network": "mainnet",
                    "threshold": 2,
                    "xpubs": self.xpubs,
                },
                "network": "mainnet",
                "l2ChainId": 38833,
                "fromBlock": 7344000,
                "toBlock": 7430399,
                "finalizedAtBlock": 7430399,
                "canonicalBridgeAddress": "kaspa:bridge",
                "canonicalBridgeScriptPublicKey": "aa20",
                "canonicalDerivationPath": "m/0/0/1",
                "evidence": second_evidence,
                "exitRequests": [],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(KaspaExitBatch.objects.count(), 2)
        self.assertNotEqual(response.json()["id"], str(first.pk))

    @mock.patch("safe_transaction_service.kaspa.serializers.get_pst_client")
    def test_create_exit_proposal_and_fetch_evidence(
        self, get_pst_client_mock: MagicMock
    ):
        federation = self.create_federation()
        exit_batch = self.create_exit_batch(federation)
        pst_client = get_pst_client_mock.return_value
        pst_client.inspect.return_value = {
            "proposalHash": "c" * 64,
            "xpubFingerprint": federation.xpub_fingerprint,
            "txIds": ["tx-unsigned"],
            "inputOutpoints": [{"txId": "prev", "index": 0, "amountSompi": 300}],
            "outputs": [{"address": "kaspa:recipient", "amountSompi": 200}],
            "feeSompi": 100,
            "mass": 1200,
            "signaturesRequired": 2,
            "signaturesCollected": 0,
            "ready": False,
        }

        response = self.client.post(
            reverse("v1:kaspa:federation-transactions", args=(federation.pk,)),
            data={
                "unsignedBundleHex": "aa",
                "exitBatch": str(exit_batch.pk),
                "proposedBy": "exit-observer",
                "origin": {"kind": "igra-l2-exit"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        proposal = KaspaTxProposal.objects.get(proposal_hash="c" * 64)
        self.assertEqual(proposal.exit_batch_id, exit_batch.id)
        exit_batch.refresh_from_db()
        self.assertEqual(exit_batch.status, KaspaExitBatchStatus.PROPOSED)
        self.assertEqual(response.json()["exitBatch"], str(exit_batch.pk))
        self.assertEqual(response.json()["exitEvidenceHash"], "e" * 64)

        pst_client.inspect.return_value = {
            "proposalHash": "d" * 64,
            "xpubFingerprint": federation.xpub_fingerprint,
            "txIds": ["tx-unsigned-2"],
            "inputOutpoints": [{"txId": "prev2", "index": 0, "amountSompi": 400}],
            "outputs": [{"address": "kaspa:recipient", "amountSompi": 200}],
            "feeSompi": 100,
            "mass": 1200,
            "signaturesRequired": 2,
            "signaturesCollected": 0,
            "ready": False,
        }
        response = self.client.post(
            reverse("v1:kaspa:federation-transactions", args=(federation.pk,)),
            data={
                "unsignedBundleHex": "bb",
                "exitBatch": str(exit_batch.pk),
                "proposedBy": "another-exit-observer",
                "origin": {"kind": "igra-l2-exit"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(KaspaTxProposal.objects.count(), 2)
        exit_batch.refresh_from_db()
        self.assertEqual(exit_batch.tx_proposals.count(), 2)

        response = self.client.get(
            reverse("v1:kaspa:exit-batch", args=(exit_batch.pk,)),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(response.json()["proposalHashes"]),
            {"c" * 64, "d" * 64},
        )

        response = self.client.get(
            reverse("v1:kaspa:exit-batch-evidence", args=(exit_batch.pk,)),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["kind"], "kaspa-exit-proposal-evidence")

    @mock.patch("safe_transaction_service.kaspa.serializers.get_pst_client")
    def test_reject_signed_bundle_when_helper_detects_mutation(
        self, get_pst_client_mock: MagicMock
    ):
        federation = self.create_federation()
        proposal = KaspaTxProposal.objects.create(
            federation=federation,
            proposal_hash="b" * 64,
            unsigned_bundle_hex="aa",
            merged_bundle_hex="aa",
            tx_ids=["tx-unsigned"],
            input_outpoints=[],
            outputs=[],
            signatures_required=2,
        )
        get_pst_client_mock.return_value.merge.side_effect = KaspaPstError(
            "outputs changed"
        )

        response = self.client.post(
            reverse("v1:kaspa:transaction-signatures", args=(proposal.proposal_hash,)),
            data={"signedBundleHex": "bb", "signer": "Mallory"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(KaspaTxSignature.objects.count(), 0)
        proposal.refresh_from_db()
        self.assertEqual(proposal.merged_bundle_hex, "aa")
