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


class KaspaExitBatchStatus(models.TextChoices):
    OBSERVING = "observing", "Observing"
    VERIFIED = "verified", "Verified"
    PROPOSED = "proposed", "Proposed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


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


class KaspaExitBatch(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    federation = models.ForeignKey(
        KaspaFederation,
        on_delete=models.CASCADE,
        related_name="exit_batches",
    )
    network = models.CharField(max_length=16, choices=KaspaNetwork.choices)
    l2_chain_id = models.PositiveIntegerField(db_index=True)
    from_block = models.PositiveBigIntegerField()
    to_block = models.PositiveBigIntegerField()
    finalized_at_block = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=KaspaExitBatchStatus.choices,
        default=KaspaExitBatchStatus.OBSERVING,
        db_index=True,
    )
    evidence_hash = models.CharField(
        max_length=64, blank=True, default="", db_index=True
    )
    total_exits = models.PositiveIntegerField(default=0)
    total_amount_sompi = models.BigIntegerField(default=0)
    canonical_bridge_address = models.CharField(max_length=128)
    canonical_bridge_script_public_key = models.CharField(
        max_length=128, blank=True, default=""
    )
    canonical_derivation_path = models.CharField(max_length=64, default="m/0/0/1")
    threshold = models.PositiveSmallIntegerField()
    xpub_fingerprint = models.CharField(max_length=64, db_index=True)
    checks = models.JSONField(default=dict)
    artifact_hashes = models.JSONField(default=dict)
    evidence = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "federation",
                    "l2_chain_id",
                    "from_block",
                    "to_block",
                    "evidence_hash",
                ],
                name="unique_kaspa_exit_batch_window",
            ),
        ]
        indexes = [
            models.Index(
                fields=["network", "status", "to_block"],
                name="kaspa_exit_batch_status",
            ),
        ]
        ordering = ["-to_block", "-created"]

    def __str__(self):
        return f"{self.l2_chain_id}:{self.from_block}-{self.to_block} {self.status}"


class KaspaExitRequest(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        KaspaExitBatch,
        on_delete=models.CASCADE,
        related_name="exit_requests",
    )
    request_id = models.PositiveBigIntegerField()
    message_id = models.CharField(max_length=66, db_index=True)
    block_number = models.PositiveBigIntegerField(db_index=True)
    transaction_hash = models.CharField(max_length=66, db_index=True)
    log_index = models.PositiveIntegerField(null=True, blank=True)
    tree_index = models.PositiveBigIntegerField(null=True, blank=True)
    recipient_address = models.CharField(max_length=128)
    amount_sompi = models.BigIntegerField()
    burn_wei = models.CharField(max_length=80, blank=True, default="")
    origin_burner_address = models.CharField(max_length=42, blank=True, default="")
    dispatch_message = models.TextField(blank=True, default="")
    dispatch_decoded = models.JSONField(default=dict)
    raw = models.JSONField(default=dict)
    checks = models.JSONField(default=dict)
    status = models.CharField(max_length=32, default="success", db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "request_id"],
                name="unique_kaspa_exit_request_id",
            ),
            models.UniqueConstraint(
                fields=["batch", "message_id"],
                name="unique_kaspa_exit_message_id",
            ),
        ]
        ordering = ["block_number", "log_index", "request_id"]

    def __str__(self):
        return f"{self.request_id} {self.amount_sompi} -> {self.recipient_address}"


class KaspaTxProposal(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    federation = models.ForeignKey(
        KaspaFederation,
        on_delete=models.CASCADE,
        related_name="tx_proposals",
    )
    exit_batch = models.ForeignKey(
        KaspaExitBatch,
        on_delete=models.PROTECT,
        related_name="tx_proposals",
        null=True,
        blank=True,
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
