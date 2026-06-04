# SPDX-License-Identifier: FSL-1.1-MIT
from django.shortcuts import get_object_or_404

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView, ListCreateAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from safe_transaction_service.history.pagination import DefaultPagination

from . import serializers
from .models import KaspaExitBatch, KaspaFederation, KaspaTxProposal


class KaspaFederationListCreateView(ListCreateAPIView):
    queryset = KaspaFederation.objects.prefetch_related("participants")
    serializer_class = serializers.KaspaFederationSerializer
    pagination_class = DefaultPagination

    @extend_schema(tags=["kaspa"], responses={201: serializers.KaspaFederationSerializer})
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    @extend_schema(tags=["kaspa"], responses={200: serializers.KaspaFederationSerializer})
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class KaspaFederationDetailView(RetrieveAPIView):
    queryset = KaspaFederation.objects.prefetch_related("participants")
    serializer_class = serializers.KaspaFederationSerializer

    @extend_schema(tags=["kaspa"], responses={200: serializers.KaspaFederationSerializer})
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class KaspaTxProposalListCreateView(ListCreateAPIView):
    pagination_class = DefaultPagination

    def get_queryset(self):
        return (
            KaspaTxProposal.objects.select_related("federation", "exit_batch")
            .prefetch_related("signatures")
            .order_by("-created")
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return serializers.KaspaTxProposalCreateSerializer
        return serializers.KaspaTxProposalResponseSerializer

    @extend_schema(
        tags=["kaspa"],
        request=serializers.KaspaTxProposalCreateSerializer,
        responses={
            201: serializers.KaspaTxProposalResponseSerializer,
            400: OpenApiResponse(description="Invalid PST bundle or federation"),
        },
    )
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proposal = serializer.save()
        return Response(
            serializers.KaspaTxProposalResponseSerializer(proposal).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(tags=["kaspa"], responses={200: serializers.KaspaTxProposalResponseSerializer})
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class KaspaFederationTransactionListCreateView(ListCreateAPIView):
    pagination_class = DefaultPagination

    def get_federation(self) -> KaspaFederation:
        if not hasattr(self, "_federation"):
            self._federation = get_object_or_404(
                KaspaFederation.objects.prefetch_related("participants"),
                pk=self.kwargs["federation_id"],
            )
        return self._federation

    def get_queryset(self):
        return (
            KaspaTxProposal.objects.filter(federation=self.get_federation())
            .select_related("exit_batch")
            .prefetch_related("signatures")
            .order_by("-created")
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return serializers.KaspaTxProposalCreateSerializer
        return serializers.KaspaTxProposalResponseSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["federation"] = self.get_federation()
        return context

    @extend_schema(
        tags=["kaspa"],
        request=serializers.KaspaTxProposalCreateSerializer,
        responses={
            201: serializers.KaspaTxProposalResponseSerializer,
            400: OpenApiResponse(description="Invalid PST bundle"),
        },
    )
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proposal = serializer.save()
        return Response(
            serializers.KaspaTxProposalResponseSerializer(proposal).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(tags=["kaspa"], responses={200: serializers.KaspaTxProposalResponseSerializer})
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class KaspaTxProposalDetailView(RetrieveAPIView):
    lookup_url_kwarg = "proposal_hash"
    lookup_field = "proposal_hash"
    queryset = KaspaTxProposal.objects.select_related(
        "federation", "exit_batch"
    ).prefetch_related("signatures")
    serializer_class = serializers.KaspaTxProposalResponseSerializer

    @extend_schema(tags=["kaspa"], responses={200: serializers.KaspaTxProposalResponseSerializer})
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class KaspaExitBatchListCreateView(ListCreateAPIView):
    queryset = (
        KaspaExitBatch.objects.select_related("federation")
        .prefetch_related("tx_proposals")
        .order_by("-to_block", "-created")
    )
    pagination_class = DefaultPagination

    def get_serializer_class(self):
        if self.request.method == "POST":
            return serializers.KaspaExitBatchCreateSerializer
        return serializers.KaspaExitBatchSummarySerializer

    @extend_schema(
        tags=["kaspa"],
        request=serializers.KaspaExitBatchCreateSerializer,
        responses={
            201: serializers.KaspaExitBatchResponseSerializer,
            400: OpenApiResponse(description="Invalid exit evidence package"),
        },
    )
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        exit_batch = serializer.save()
        return Response(
            serializers.KaspaExitBatchResponseSerializer(exit_batch).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        tags=["kaspa"], responses={200: serializers.KaspaExitBatchSummarySerializer}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class KaspaExitBatchDetailView(RetrieveAPIView):
    queryset = (
        KaspaExitBatch.objects.select_related("federation")
        .prefetch_related("exit_requests", "tx_proposals")
        .order_by("-to_block", "-created")
    )
    serializer_class = serializers.KaspaExitBatchResponseSerializer

    @extend_schema(
        tags=["kaspa"], responses={200: serializers.KaspaExitBatchResponseSerializer}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class KaspaExitBatchEvidenceView(APIView):
    @extend_schema(
        tags=["kaspa"],
        responses={200: OpenApiResponse(description="Verified exit evidence package")},
    )
    def get(self, request, pk, *args, **kwargs):
        exit_batch = get_object_or_404(KaspaExitBatch, pk=pk)
        return Response(exit_batch.evidence)


class KaspaTxSignatureListCreateView(ListCreateAPIView):
    pagination_class = DefaultPagination

    def get_proposal(self) -> KaspaTxProposal:
        if not hasattr(self, "_proposal"):
            self._proposal = get_object_or_404(
                KaspaTxProposal.objects.select_related("federation"),
                proposal_hash=self.kwargs["proposal_hash"],
            )
        return self._proposal

    def get_queryset(self):
        return self.get_proposal().signatures.select_related("participant")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return serializers.KaspaTxSignatureCreateSerializer
        return serializers.KaspaTxSignatureResponseSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["proposal"] = self.get_proposal()
        return context

    @extend_schema(tags=["kaspa"], responses={200: serializers.KaspaTxSignatureResponseSerializer})
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=["kaspa"],
        request=serializers.KaspaTxSignatureCreateSerializer,
        responses={
            201: serializers.KaspaTxProposalResponseSerializer,
            400: OpenApiResponse(description="Invalid signed PST bundle"),
        },
    )
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        signature = serializer.save()
        proposal = (
            KaspaTxProposal.objects.select_related("federation")
            .select_related("exit_batch")
            .prefetch_related("signatures")
            .get(pk=signature.proposal_id)
        )
        return Response(
            serializers.KaspaTxProposalResponseSerializer(proposal).data,
            status=status.HTTP_201_CREATED,
        )


class KaspaTxBroadcastView(APIView):
    @extend_schema(
        tags=["kaspa"],
        request=serializers.KaspaBroadcastSerializer,
        responses={
            200: serializers.KaspaTxProposalResponseSerializer,
            400: OpenApiResponse(description="Proposal is not ready or broadcast failed"),
        },
    )
    def post(self, request, proposal_hash, *args, **kwargs):
        proposal = get_object_or_404(
            KaspaTxProposal.objects.select_related("federation", "exit_batch"),
            proposal_hash=proposal_hash,
        )
        serializer = serializers.KaspaBroadcastSerializer(
            data=request.data,
            context={"proposal": proposal},
        )
        serializer.is_valid(raise_exception=True)
        proposal = serializer.save()
        return Response(serializers.KaspaTxProposalResponseSerializer(proposal).data)
