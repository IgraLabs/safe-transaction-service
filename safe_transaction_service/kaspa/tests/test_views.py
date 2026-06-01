# SPDX-License-Identifier: FSL-1.1-MIT
from unittest import mock
from unittest.mock import MagicMock

from django.urls import reverse

from rest_framework import status
from rest_framework.test import APITestCase

from safe_transaction_service.kaspa.models import (
    KaspaBroadcastAttempt,
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
