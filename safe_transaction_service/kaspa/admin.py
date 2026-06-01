# SPDX-License-Identifier: FSL-1.1-MIT
from django.contrib import admin

from .models import (
    KaspaBroadcastAttempt,
    KaspaFederation,
    KaspaFederationParticipant,
    KaspaTxProposal,
    KaspaTxSignature,
)


@admin.register(KaspaFederation)
class KaspaFederationAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "network", "threshold", "xpub_fingerprint")
    search_fields = ("id", "name", "xpub_fingerprint")


@admin.register(KaspaFederationParticipant)
class KaspaFederationParticipantAdmin(admin.ModelAdmin):
    list_display = ("id", "federation", "name", "cosigner_index")
    search_fields = ("id", "name", "root_xpub")


@admin.register(KaspaTxProposal)
class KaspaTxProposalAdmin(admin.ModelAdmin):
    list_display = (
        "proposal_hash",
        "federation",
        "status",
        "signatures_collected",
        "signatures_required",
        "created",
    )
    list_filter = ("status", "federation__network")
    search_fields = ("proposal_hash",)


@admin.register(KaspaTxSignature)
class KaspaTxSignatureAdmin(admin.ModelAdmin):
    list_display = ("id", "proposal", "participant", "signer", "signature_count")
    search_fields = ("id", "proposal__proposal_hash", "signer", "submission_hash")


@admin.register(KaspaBroadcastAttempt)
class KaspaBroadcastAttemptAdmin(admin.ModelAdmin):
    list_display = ("id", "proposal", "success", "rpc_url", "created")
    list_filter = ("success",)
    search_fields = ("id", "proposal__proposal_hash")
