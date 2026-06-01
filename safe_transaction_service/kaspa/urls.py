# SPDX-License-Identifier: FSL-1.1-MIT
from django.urls import path

from . import views

app_name = "kaspa"

urlpatterns = [
    path(
        "federations/",
        views.KaspaFederationListCreateView.as_view(),
        name="federations",
    ),
    path(
        "federations/<uuid:pk>/",
        views.KaspaFederationDetailView.as_view(),
        name="federation",
    ),
    path(
        "federations/<uuid:federation_id>/transactions/",
        views.KaspaFederationTransactionListCreateView.as_view(),
        name="federation-transactions",
    ),
    path(
        "transactions/<str:proposal_hash>/",
        views.KaspaTxProposalDetailView.as_view(),
        name="transaction",
    ),
    path(
        "exit-batches/",
        views.KaspaExitBatchListView.as_view(),
        name="exit-batches",
    ),
    path(
        "exit-batches/<uuid:pk>/",
        views.KaspaExitBatchDetailView.as_view(),
        name="exit-batch",
    ),
    path(
        "exit-batches/<uuid:pk>/evidence/",
        views.KaspaExitBatchEvidenceView.as_view(),
        name="exit-batch-evidence",
    ),
    path(
        "transactions/<str:proposal_hash>/signatures/",
        views.KaspaTxSignatureListCreateView.as_view(),
        name="transaction-signatures",
    ),
    path(
        "transactions/<str:proposal_hash>/broadcast/",
        views.KaspaTxBroadcastView.as_view(),
        name="transaction-broadcast",
    ),
]
