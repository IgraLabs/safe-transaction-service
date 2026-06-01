# SPDX-License-Identifier: FSL-1.1-MIT
import uuid

from django.db import models

from model_utils.models import TimeStampedModel


class KaspaNetwork(models.TextChoices):
    MAINNET = "mainnet", "Mainnet"
    TESTNET = "testnet", "Testnet"
    DEVNET = "devnet", "Devnet"
    SIMNET = "simnet", "Simnet"


class KaspaTxProposalStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    READY = "ready", "Ready"
    BROADCASTED = "broadcasted", "Broadcasted"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"


class KaspaFederation(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128, blank=True, default="")
    network = models.CharField(max_length=16, choices=KaspaNetwork.choices)
    xpubs = models.JSONField(default=list)
    xpub_fingerprint = models.CharField(max_length=64, db_index=True)
    threshold = models.PositiveSmallIntegerField()
    ecdsa = models.BooleanField(default=False)
    origin = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["network", "xpub_fingerprint", "threshold", "ecdsa"],
                name="unique_kaspa_federation",
            )
        ]
        ordering = ["created"]

    def __str__(self):
        return f"{self.network} {self.threshold}-of-{len(self.xpubs)} {self.xpub_fingerprint[:8]}"


class KaspaFederationParticipant(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    federation = models.ForeignKey(
        KaspaFederation,
        on_delete=models.CASCADE,
        related_name="participants",
    )
    name = models.CharField(max_length=128, blank=True, default="")
    root_xpub = models.TextField()
    cosigner_index = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["federation", "root_xpub"],
                name="unique_kaspa_participant_xpub",
            ),
            models.UniqueConstraint(
                fields=["federation", "cosigner_index"],
                name="unique_kaspa_participant_cosigner_index",
            ),
        ]
        ordering = ["cosigner_index"]

    def __str__(self):
        label = self.name or self.root_xpub[:16]
        return f"{label} ({self.cosigner_index})"


class KaspaTxProposal(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    federation = models.ForeignKey(
        KaspaFederation,
        on_delete=models.CASCADE,
        related_name="tx_proposals",
    )
    proposal_hash = models.CharField(max_length=64, unique=True, db_index=True)
    format = models.CharField(max_length=32, default="kaspawallet_pst_v1")
    unsigned_bundle_hex = models.TextField()
    merged_bundle_hex = models.TextField()
    status = models.CharField(
        max_length=16,
        choices=KaspaTxProposalStatus.choices,
        default=KaspaTxProposalStatus.PENDING,
        db_index=True,
    )
    tx_ids = models.JSONField(default=list)
    input_outpoints = models.JSONField(default=list)
    outputs = models.JSONField(default=list)
    fee_sompi = models.BigIntegerField(null=True, blank=True)
    mass = models.BigIntegerField(null=True, blank=True)
    signatures_required = models.PositiveSmallIntegerField()
    signatures_collected = models.PositiveSmallIntegerField(default=0)
    proposed_by = models.CharField(max_length=255, blank=True, default="")
    origin = models.JSONField(default=dict)
    broadcast_tx_ids = models.JSONField(default=list)
    broadcast_error = models.TextField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["federation", "status", "-created"],
                name="kaspa_proposal_fed_status",
            ),
        ]
        ordering = ["-created"]

    def __str__(self):
        return f"{self.proposal_hash} - {self.status}"


class KaspaTxSignature(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal = models.ForeignKey(
        KaspaTxProposal,
        on_delete=models.CASCADE,
        related_name="signatures",
    )
    participant = models.ForeignKey(
        KaspaFederationParticipant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tx_signatures",
    )
    signer = models.CharField(max_length=255, blank=True, default="")
    submission_hash = models.CharField(max_length=64)
    signed_bundle_hex = models.TextField()
    added_signatures = models.JSONField(default=list)
    signature_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["proposal", "submission_hash"],
                name="unique_kaspa_signature_submission",
            ),
        ]
        ordering = ["created"]

    def __str__(self):
        return f"{self.proposal_id} - {self.signature_count} signatures"


class KaspaBroadcastAttempt(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal = models.ForeignKey(
        KaspaTxProposal,
        on_delete=models.CASCADE,
        related_name="broadcast_attempts",
    )
    tx_ids = models.JSONField(default=list)
    rpc_url = models.CharField(max_length=255, blank=True, default="")
    error = models.TextField(null=True, blank=True)
    success = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"{self.proposal_id} - {'success' if self.success else 'failed'}"
