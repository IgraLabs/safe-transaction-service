# SPDX-License-Identifier: FSL-1.1-MIT
import hashlib
import json
import re
from typing import Any

from django.db import IntegrityError, transaction

from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from .models import (
    KaspaBroadcastAttempt,
    KaspaExitBatch,
    KaspaExitBatchStatus,
    KaspaExitRequest,
    KaspaFederation,
    KaspaFederationParticipant,
    KaspaNetwork,
    KaspaTxProposal,
    KaspaTxProposalStatus,
    KaspaTxSignature,
)
from .services.pst import KaspaPstError, get_pst_client

KASPAWALLET_BUNDLE_RE = re.compile(r"^[0-9a-fA-F]+(?:_[0-9a-fA-F]+)*$")


def canonical_json_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize_bundle_hex(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValidationError("Bundle hex cannot be empty")
    if value.startswith("0x"):
        raise ValidationError("Bundle hex must not include a 0x prefix")
    if not KASPAWALLET_BUNDLE_RE.fullmatch(value):
        raise ValidationError("Bundle hex must be hex transactions separated by '_'")
    return value.lower()


def normalize_xpubs(xpubs: list[str]) -> list[str]:
    normalized = [xpub.strip() for xpub in xpubs]
    if any(not xpub for xpub in normalized):
        raise ValidationError("Xpubs cannot be empty")
    if len(set(normalized)) != len(normalized):
        raise ValidationError("Xpubs must be unique")
    return sorted(normalized)


class KaspaFederationParticipantInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=128, allow_blank=True, default="")
    xpub = serializers.CharField()


class KaspaFederationParticipantResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    root_xpub = serializers.CharField()
    cosigner_index = serializers.IntegerField()


class KaspaFederationSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    created = serializers.DateTimeField(read_only=True)
    modified = serializers.DateTimeField(read_only=True)
    name = serializers.CharField(max_length=128, allow_blank=True, default="")
    network = serializers.ChoiceField(choices=KaspaNetwork.choices)
    threshold = serializers.IntegerField(min_value=1)
    ecdsa = serializers.BooleanField(default=False)
    xpubs = serializers.ListField(child=serializers.CharField(), min_length=1)
    xpub_fingerprint = serializers.CharField(read_only=True)
    participants = KaspaFederationParticipantInputSerializer(
        many=True, required=False, write_only=True
    )
    participant_details = serializers.SerializerMethodField(read_only=True)
    origin = serializers.JSONField(default=dict)

    def get_participant_details(self, obj: KaspaFederation) -> list[dict[str, Any]]:
        return KaspaFederationParticipantResponseSerializer(
            obj.participants.all(), many=True
        ).data

    def validate(self, attrs):
        attrs = super().validate(attrs)
        xpubs = normalize_xpubs(attrs["xpubs"])
        threshold = attrs["threshold"]
        if threshold > len(xpubs):
            raise ValidationError("Threshold cannot be greater than number of xpubs")

        participants = attrs.get("participants") or []
        participant_xpubs = [participant["xpub"].strip() for participant in participants]
        unknown_xpubs = set(participant_xpubs) - set(xpubs)
        if unknown_xpubs:
            raise ValidationError("Participants contain xpubs not present in federation")
        if len(set(participant_xpubs)) != len(participant_xpubs):
            raise ValidationError("Participant xpubs must be unique")

        attrs["xpubs"] = xpubs
        attrs["xpub_fingerprint"] = canonical_json_hash(xpubs)
        return attrs

    def create(self, validated_data):
        participants = validated_data.pop("participants", [])
        participant_names = {
            participant["xpub"].strip(): participant.get("name", "")
            for participant in participants
        }

        try:
            with transaction.atomic():
                federation = KaspaFederation.objects.create(**validated_data)
                participant_objects = [
                    KaspaFederationParticipant(
                        federation=federation,
                        root_xpub=xpub,
                        cosigner_index=cosigner_index,
                        name=participant_names.get(xpub, ""),
                    )
                    for cosigner_index, xpub in enumerate(federation.xpubs)
                ]
                KaspaFederationParticipant.objects.bulk_create(participant_objects)
        except IntegrityError as exc:
            raise ValidationError("Kaspa federation already exists") from exc

        return federation


class KaspaTxSignatureResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    created = serializers.DateTimeField()
    modified = serializers.DateTimeField()
    participant = serializers.UUIDField(source="participant_id", allow_null=True)
    signer = serializers.CharField()
    submission_hash = serializers.CharField()
    added_signatures = serializers.JSONField()
    signature_count = serializers.IntegerField()


class KaspaExitRequestResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    created = serializers.DateTimeField()
    modified = serializers.DateTimeField()
    request_id = serializers.IntegerField()
    message_id = serializers.CharField()
    block_number = serializers.IntegerField()
    transaction_hash = serializers.CharField()
    log_index = serializers.IntegerField(allow_null=True)
    tree_index = serializers.IntegerField(allow_null=True)
    recipient_address = serializers.CharField()
    amount_sompi = serializers.IntegerField()
    burn_wei = serializers.CharField()
    origin_burner_address = serializers.CharField()
    dispatch_message = serializers.CharField()
    dispatch_decoded = serializers.JSONField()
    raw = serializers.JSONField()
    checks = serializers.JSONField()
    status = serializers.CharField()


class KaspaExitBatchSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    created = serializers.DateTimeField()
    modified = serializers.DateTimeField()
    federation = serializers.UUIDField(source="federation_id")
    network = serializers.CharField()
    l2_chain_id = serializers.IntegerField()
    from_block = serializers.IntegerField()
    to_block = serializers.IntegerField()
    finalized_at_block = serializers.IntegerField(allow_null=True)
    status = serializers.CharField()
    evidence_hash = serializers.CharField()
    total_exits = serializers.IntegerField()
    total_amount_sompi = serializers.IntegerField()
    canonical_bridge_address = serializers.CharField()
    threshold = serializers.IntegerField()
    xpub_fingerprint = serializers.CharField()
    proposal_hash = serializers.SerializerMethodField()

    def get_proposal_hash(self, obj: KaspaExitBatch) -> str | None:
        try:
            return obj.tx_proposal.proposal_hash
        except KaspaTxProposal.DoesNotExist:
            return None


class KaspaExitBatchResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    created = serializers.DateTimeField()
    modified = serializers.DateTimeField()
    federation = serializers.UUIDField(source="federation_id")
    network = serializers.CharField()
    l2_chain_id = serializers.IntegerField()
    from_block = serializers.IntegerField()
    to_block = serializers.IntegerField()
    finalized_at_block = serializers.IntegerField(allow_null=True)
    status = serializers.CharField()
    evidence_hash = serializers.CharField()
    total_exits = serializers.IntegerField()
    total_amount_sompi = serializers.IntegerField()
    canonical_bridge_address = serializers.CharField()
    canonical_bridge_script_public_key = serializers.CharField()
    canonical_derivation_path = serializers.CharField()
    threshold = serializers.IntegerField()
    xpub_fingerprint = serializers.CharField()
    checks = serializers.JSONField()
    artifact_hashes = serializers.JSONField()
    evidence = serializers.JSONField()
    proposal_hash = serializers.SerializerMethodField()
    exit_requests = serializers.SerializerMethodField()

    def get_proposal_hash(self, obj: KaspaExitBatch) -> str | None:
        try:
            return obj.tx_proposal.proposal_hash
        except KaspaTxProposal.DoesNotExist:
            return None

    def get_exit_requests(self, obj: KaspaExitBatch) -> list[dict[str, Any]]:
        return KaspaExitRequestResponseSerializer(
            obj.exit_requests.all(), many=True
        ).data


class KaspaTxProposalResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    created = serializers.DateTimeField()
    modified = serializers.DateTimeField()
    federation = serializers.UUIDField(source="federation_id")
    exit_batch = serializers.SerializerMethodField()
    exit_evidence_hash = serializers.SerializerMethodField()
    proposal_hash = serializers.CharField()
    format = serializers.CharField()
    unsigned_bundle_hex = serializers.CharField()
    merged_bundle_hex = serializers.CharField()
    status = serializers.CharField()
    tx_ids = serializers.JSONField()
    input_outpoints = serializers.JSONField()
    outputs = serializers.JSONField()
    fee_sompi = serializers.IntegerField(allow_null=True)
    mass = serializers.IntegerField(allow_null=True)
    signatures_required = serializers.IntegerField()
    signatures_collected = serializers.IntegerField()
    proposed_by = serializers.CharField()
    origin = serializers.JSONField()
    broadcast_tx_ids = serializers.JSONField()
    broadcast_error = serializers.CharField(allow_null=True)
    signatures = serializers.SerializerMethodField()

    def get_signatures(self, obj: KaspaTxProposal) -> list[dict[str, Any]]:
        return KaspaTxSignatureResponseSerializer(obj.signatures.all(), many=True).data

    def get_exit_batch(self, obj: KaspaTxProposal) -> str | None:
        if not obj.exit_batch_id:
            return None
        return str(obj.exit_batch_id)

    def get_exit_evidence_hash(self, obj: KaspaTxProposal) -> str | None:
        if not obj.exit_batch_id:
            return None
        return obj.exit_batch.evidence_hash


class KaspaTxProposalCreateSerializer(serializers.Serializer):
    unsigned_bundle_hex = serializers.CharField()
    exit_batch = serializers.UUIDField(required=False, allow_null=True)
    proposed_by = serializers.CharField(max_length=255, allow_blank=True, default="")
    origin = serializers.JSONField(default=dict)

    def validate_unsigned_bundle_hex(self, value: str) -> str:
        return normalize_bundle_hex(value)

    def validate_exit_batch(self, value) -> KaspaExitBatch | None:
        if value is None:
            return None

        federation: KaspaFederation = self.context["federation"]
        try:
            exit_batch = KaspaExitBatch.objects.get(pk=value, federation=federation)
        except KaspaExitBatch.DoesNotExist as exc:
            raise ValidationError(
                "Exit batch does not belong to this federation"
            ) from exc

        if exit_batch.status != KaspaExitBatchStatus.VERIFIED:
            raise ValidationError(
                "Exit batch must be verified before proposal creation"
            )
        if exit_batch.network != federation.network:
            raise ValidationError("Exit batch network does not match federation")
        if exit_batch.threshold != federation.threshold:
            raise ValidationError("Exit batch threshold does not match federation")
        if exit_batch.xpub_fingerprint != federation.xpub_fingerprint:
            raise ValidationError(
                "Exit batch xpub fingerprint does not match federation"
            )
        if hasattr(exit_batch, "tx_proposal"):
            raise ValidationError("Exit batch already has a transaction proposal")

        return exit_batch

    def validate(self, attrs):
        attrs = super().validate(attrs)
        federation: KaspaFederation = self.context["federation"]
        try:
            inspection = get_pst_client().inspect(
                attrs["unsigned_bundle_hex"],
                federation.network,
                federation_xpubs=federation.xpubs,
                threshold=federation.threshold,
                ecdsa=federation.ecdsa,
            )
        except KaspaPstError as exc:
            raise ValidationError(str(exc)) from exc

        proposal_hash = inspection.get("proposalHash")
        if not proposal_hash:
            proposal_hash = canonical_json_hash(
                {
                    "network": federation.network,
                    "bundle": attrs["unsigned_bundle_hex"],
                    "inspection": inspection,
                }
            )

        tx_fingerprint = inspection.get("xpubFingerprint")
        if tx_fingerprint and tx_fingerprint != federation.xpub_fingerprint:
            raise ValidationError("Bundle xpub fingerprint does not match federation")

        signatures_required = inspection.get("signaturesRequired", federation.threshold)
        if signatures_required != federation.threshold:
            raise ValidationError("Bundle threshold does not match federation")

        attrs["inspection"] = inspection
        attrs["proposal_hash"] = proposal_hash
        return attrs

    def create(self, validated_data):
        federation: KaspaFederation = self.context["federation"]
        inspection = validated_data.pop("inspection")
        exit_batch = validated_data.get("exit_batch")
        unsigned_bundle_hex = validated_data["unsigned_bundle_hex"]
        ready = bool(inspection.get("ready", False))

        try:
            with transaction.atomic():
                proposal = KaspaTxProposal.objects.create(
                    federation=federation,
                    exit_batch=exit_batch,
                    proposal_hash=validated_data["proposal_hash"],
                    unsigned_bundle_hex=unsigned_bundle_hex,
                    merged_bundle_hex=unsigned_bundle_hex,
                    status=(
                        KaspaTxProposalStatus.READY
                        if ready
                        else KaspaTxProposalStatus.PENDING
                    ),
                    tx_ids=inspection.get("txIds", []),
                    input_outpoints=inspection.get("inputOutpoints", []),
                    outputs=inspection.get("outputs", []),
                    fee_sompi=inspection.get("feeSompi"),
                    mass=inspection.get("mass"),
                    signatures_required=inspection.get(
                        "signaturesRequired", federation.threshold
                    ),
                    signatures_collected=inspection.get("signaturesCollected", 0),
                    proposed_by=validated_data.get("proposed_by", ""),
                    origin=validated_data.get("origin", {}),
                )
                if exit_batch:
                    exit_batch.status = KaspaExitBatchStatus.PROPOSED
                    exit_batch.save(update_fields=["status", "modified"])
                return proposal
        except IntegrityError as exc:
            raise ValidationError("Kaspa transaction proposal already exists") from exc


class KaspaTxSignatureCreateSerializer(serializers.Serializer):
    signed_bundle_hex = serializers.CharField()
    participant = serializers.UUIDField(required=False, allow_null=True)
    signer = serializers.CharField(max_length=255, allow_blank=True, default="")

    def validate_signed_bundle_hex(self, value: str) -> str:
        return normalize_bundle_hex(value)

    def validate_participant(self, value):
        if value is None:
            return None
        proposal: KaspaTxProposal = self.context["proposal"]
        try:
            return proposal.federation.participants.get(pk=value)
        except KaspaFederationParticipant.DoesNotExist as exc:
            raise ValidationError("Participant does not belong to this federation") from exc

    def create(self, validated_data):
        proposal: KaspaTxProposal = self.context["proposal"]
        signed_bundle_hex = validated_data["signed_bundle_hex"]
        participant = validated_data.get("participant")
        signer = validated_data.get("signer", "")

        with transaction.atomic():
            proposal = (
                KaspaTxProposal.objects.select_for_update()
                .select_related("federation")
                .get(pk=proposal.pk)
            )
            if proposal.status in (
                KaspaTxProposalStatus.BROADCASTED,
                KaspaTxProposalStatus.CANCELLED,
            ):
                raise ValidationError(
                    f"Cannot add signatures to a {proposal.status} proposal"
                )

            try:
                merge_result = get_pst_client().merge(
                    proposal.merged_bundle_hex,
                    signed_bundle_hex,
                    proposal.federation.network,
                    federation_xpubs=proposal.federation.xpubs,
                    threshold=proposal.federation.threshold,
                    ecdsa=proposal.federation.ecdsa,
                )
            except KaspaPstError as exc:
                raise ValidationError(str(exc)) from exc

            added_signatures = merge_result.get("addedSignatures", [])
            if not added_signatures:
                raise ValidationError("Signed bundle did not add any new signatures")
            merged_bundle_hex = merge_result.get("mergedBundleHex")
            if not merged_bundle_hex:
                raise ValidationError("PST helper did not return merged bundle hex")

            submission_hash = canonical_json_hash(
                {
                    "proposalHash": proposal.proposal_hash,
                    "signer": signer,
                    "participant": str(participant.pk) if participant else None,
                    "addedSignatures": added_signatures,
                }
            )
            try:
                kaspa_signature = KaspaTxSignature.objects.create(
                    proposal=proposal,
                    participant=participant,
                    signer=signer,
                    submission_hash=submission_hash,
                    signed_bundle_hex=signed_bundle_hex,
                    added_signatures=added_signatures,
                    signature_count=len(added_signatures),
                )
            except IntegrityError as exc:
                raise ValidationError("Kaspa signature submission already exists") from exc

            proposal.merged_bundle_hex = merged_bundle_hex
            proposal.signatures_collected = merge_result.get(
                "signaturesCollected", proposal.signatures_collected
            )
            proposal.tx_ids = merge_result.get("txIds", proposal.tx_ids)
            proposal.status = (
                KaspaTxProposalStatus.READY
                if merge_result.get("ready", False)
                else KaspaTxProposalStatus.PENDING
            )
            proposal.save(
                update_fields=[
                    "merged_bundle_hex",
                    "signatures_collected",
                    "tx_ids",
                    "status",
                    "modified",
                ]
            )
            return kaspa_signature


class KaspaBroadcastSerializer(serializers.Serializer):
    rpc_url = serializers.CharField(max_length=255, allow_blank=True, default="")

    def create(self, validated_data):
        proposal: KaspaTxProposal = self.context["proposal"]
        if proposal.status != KaspaTxProposalStatus.READY:
            raise ValidationError("Only ready Kaspa proposals can be broadcast")

        rpc_url = validated_data.get("rpc_url", "")
        try:
            broadcast_result = get_pst_client().broadcast(
                proposal.merged_bundle_hex,
                proposal.federation.network,
                rpc_url=rpc_url or None,
                federation_xpubs=proposal.federation.xpubs,
                threshold=proposal.federation.threshold,
                ecdsa=proposal.federation.ecdsa,
            )
        except KaspaPstError as exc:
            KaspaBroadcastAttempt.objects.create(
                proposal=proposal,
                rpc_url=rpc_url,
                error=str(exc),
                success=False,
            )
            KaspaTxProposal.objects.filter(pk=proposal.pk).update(
                status=KaspaTxProposalStatus.FAILED,
                broadcast_error=str(exc),
            )
            raise ValidationError(str(exc)) from exc

        tx_ids = broadcast_result.get("txIds", [])
        KaspaBroadcastAttempt.objects.create(
            proposal=proposal,
            rpc_url=rpc_url,
            tx_ids=tx_ids,
            success=True,
        )
        KaspaTxProposal.objects.filter(pk=proposal.pk).update(
            status=KaspaTxProposalStatus.BROADCASTED,
            broadcast_tx_ids=tx_ids,
            broadcast_error=None,
        )
        proposal.refresh_from_db()
        return proposal
