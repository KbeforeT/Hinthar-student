from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from people.audit import AuditedViewSetMixin, log_event
from people.models import Teacher
from people.permissions import IsStaffOrAbove

from .models import (
    CohortRateOverride,
    TeacherPayout,
    TeacherRateCard,
    sync_teacher_payout,
)
from .serializers import (
    CohortRateOverrideSerializer,
    TeacherPayoutSerializer,
    TeacherRateCardSerializer,
)


def _parse_month_param(raw: str | None) -> date:
    if not raw:
        return timezone.localdate().replace(day=1)
    cleaned = raw.strip()
    if len(cleaned) == 7 and "-" in cleaned:
        cleaned = f"{cleaned}-01"
    try:
        parsed = date.fromisoformat(cleaned[:10])
    except ValueError as exc:
        raise serializers.ValidationError({"month": "Invalid month format. Use YYYY-MM or YYYY-MM-DD."}) from exc
    return parsed.replace(day=1)


def _parse_decimal_or_none(raw, field_name: str):
    if raw is None or raw == "":
        return None
    try:
        val = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise serializers.ValidationError({field_name: "Must be a valid number."}) from exc
    if val < Decimal("0.00"):
        raise serializers.ValidationError({field_name: "Cannot be negative."})
    return val


class TeacherRateCardViewSet(AuditedViewSetMixin, viewsets.ModelViewSet):
    queryset = (
        TeacherRateCard.objects.select_related("teacher")
        .prefetch_related("cohort_overrides__class_obj")
        .order_by("teacher__name", "pk")
    )
    serializer_class = TeacherRateCardSerializer
    permission_classes = [IsStaffOrAbove]
    audit_category = "teacher"
    audit_noun = "teacher rate card"
    pagination_class = None

    def get_queryset(self):
        qs = super().get_queryset()
        teacher_id = self.request.query_params.get("teacher_id")
        if teacher_id and teacher_id != "all":
            try:
                qs = qs.filter(teacher_id=int(teacher_id))
            except (TypeError, ValueError) as exc:
                raise serializers.ValidationError({"teacher_id": "Must be a positive integer."}) from exc
        return qs

    @action(detail=False, methods=["post"])
    def upsert(self, request):
        teacher_id = request.data.get("teacher_id") or request.data.get("teacher")
        if not teacher_id:
            raise serializers.ValidationError({"teacher_id": "teacher_id is required."})
        try:
            teacher = Teacher.objects.get(pk=int(teacher_id))
        except (Teacher.DoesNotExist, TypeError, ValueError) as exc:
            raise serializers.ValidationError({"teacher_id": "Teacher not found."}) from exc

        rate_type = request.data.get("rate_type") or TeacherRateCard.RateType.PER_SESSION
        if rate_type not in TeacherRateCard.RateType.values:
            raise serializers.ValidationError({"rate_type": "Invalid rate_type."})

        rate_per_session = _parse_decimal_or_none(request.data.get("rate_per_session"), "rate_per_session")
        hourly_rate = _parse_decimal_or_none(request.data.get("hourly_rate"), "hourly_rate")
        currency = (request.data.get("currency") or "MMK").strip() or "MMK"
        notes = (request.data.get("notes") or "").strip()
        overrides = request.data.get("cohort_overrides")

        with transaction.atomic():
            card, _created = TeacherRateCard.objects.get_or_create(teacher=teacher)
            card.rate_type = rate_type
            card.rate_per_session = rate_per_session if rate_per_session is not None else Decimal("0.00")
            card.hourly_rate = hourly_rate if hourly_rate is not None else Decimal("0.00")
            card.currency = currency
            card.notes = notes
            card.save()

            if isinstance(overrides, list):
                keep_class_ids: set[int] = set()
                for idx, ov in enumerate(overrides):
                    if not isinstance(ov, dict):
                        raise serializers.ValidationError(
                            {"cohort_overrides": f"Override #{idx + 1} must be an object."}
                        )
                    raw_cid = ov.get("class_obj_id") or ov.get("class_obj")
                    try:
                        cid = int(raw_cid)
                    except (TypeError, ValueError) as exc:
                        raise serializers.ValidationError(
                            {"cohort_overrides": f"Override #{idx + 1}: valid class_obj_id required."}
                        ) from exc
                    ov_rps = _parse_decimal_or_none(ov.get("rate_per_session"), "rate_per_session")
                    ov_hr = _parse_decimal_or_none(ov.get("hourly_rate"), "hourly_rate")
                    if ov_rps is None and ov_hr is None:
                        raise serializers.ValidationError(
                            {"cohort_overrides": f"Override #{idx + 1}: provide rate_per_session or hourly_rate."}
                        )
                    keep_class_ids.add(cid)
                    CohortRateOverride.objects.update_or_create(
                        rate_card=card,
                        class_obj_id=cid,
                        defaults={"rate_per_session": ov_rps, "hourly_rate": ov_hr},
                    )
                card.cohort_overrides.exclude(class_obj_id__in=keep_class_ids).delete()

        # Refresh any draft payouts for this teacher so numbers stay accurate immediately.
        for draft in TeacherPayout.objects.filter(teacher=teacher, status=TeacherPayout.Status.DRAFT):
            sync_teacher_payout(teacher, draft.month, actor=request.user)

        card = (
            TeacherRateCard.objects.select_related("teacher")
            .prefetch_related("cohort_overrides__class_obj")
            .get(pk=card.pk)
        )
        return Response(self.get_serializer(card).data, status=status.HTTP_200_OK)


class CohortRateOverrideViewSet(AuditedViewSetMixin, viewsets.ModelViewSet):
    queryset = CohortRateOverride.objects.select_related("rate_card__teacher", "class_obj").all()
    serializer_class = CohortRateOverrideSerializer
    permission_classes = [IsStaffOrAbove]
    audit_category = "teacher"
    audit_noun = "cohort rate override"
    pagination_class = None

    def get_queryset(self):
        qs = super().get_queryset()
        rate_card_id = self.request.query_params.get("rate_card_id")
        if rate_card_id:
            try:
                qs = qs.filter(rate_card_id=int(rate_card_id))
            except (TypeError, ValueError) as exc:
                raise serializers.ValidationError({"rate_card_id": "Must be a positive integer."}) from exc
        return qs


class TeacherPayoutViewSet(AuditedViewSetMixin, viewsets.ModelViewSet):
    queryset = (
        TeacherPayout.objects.select_related("teacher", "teacher__rate_card")
        .prefetch_related("teacher__rate_card__cohort_overrides")
        .order_by("-month", "teacher__name", "pk")
    )
    serializer_class = TeacherPayoutSerializer
    permission_classes = [IsStaffOrAbove]
    audit_category = "teacher"
    audit_noun = "teacher payout"
    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]

    def _sync_month_if_requested(self, month_date: date, actor):
        teachers = list(Teacher.objects.order_by("id"))
        for teacher in teachers:
            sync_teacher_payout(teacher, month_date, actor=actor)

    def list(self, request, *args, **kwargs):
        raw_month = request.query_params.get("month")
        auto_sync = (request.query_params.get("sync") or "true").strip().lower() != "false"
        if auto_sync:
            month_date = _parse_month_param(raw_month)
            self._sync_month_if_requested(month_date, request.user)
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        qs = super().get_queryset()
        raw_month = self.request.query_params.get("month")
        if raw_month:
            month_date = _parse_month_param(raw_month)
            qs = qs.filter(month=month_date)
        teacher_id = self.request.query_params.get("teacher_id")
        if teacher_id and teacher_id != "all":
            try:
                qs = qs.filter(teacher_id=int(teacher_id))
            except (TypeError, ValueError) as exc:
                raise serializers.ValidationError({"teacher_id": "Must be a positive integer."}) from exc
        status_param = (self.request.query_params.get("status") or "").strip().lower()
        if status_param and status_param != "all":
            if status_param not in TeacherPayout.Status.values:
                raise serializers.ValidationError({"status": "Invalid payout status."})
            qs = qs.filter(status=status_param)
        q = (self.request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(teacher__name__icontains=q) | Q(teacher__unique_code__icontains=q))
        return qs

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)
        self._log_instance("update", serializer.instance)

    @action(detail=False, methods=["post"])
    def calculate(self, request):
        month_date = _parse_month_param(request.data.get("month") or request.query_params.get("month"))
        force = bool(request.data.get("force", False))
        raw_teacher_id = request.data.get("teacher_id")

        if raw_teacher_id:
            try:
                teachers = [Teacher.objects.get(pk=int(raw_teacher_id))]
            except (Teacher.DoesNotExist, TypeError, ValueError) as exc:
                raise serializers.ValidationError({"teacher_id": "Teacher not found."}) from exc
        else:
            teachers = list(Teacher.objects.order_by("name", "id"))

        payouts = [
            sync_teacher_payout(
                t,
                month_date,
                actor=request.user,
                force_recalculate=force,
            )
            for t in teachers
        ]
        # Re-query with select_related / prefetch_related
        ids = [p.pk for p in payouts]
        refreshed = (
            TeacherPayout.objects.filter(pk__in=ids)
            .select_related("teacher", "teacher__rate_card")
            .prefetch_related("teacher__rate_card__cohort_overrides")
            .order_by("teacher__name", "pk")
        )
        return Response(
            {
                "month": month_date.isoformat(),
                "calculated_count": len(ids),
                "payouts": TeacherPayoutSerializer(refreshed, many=True).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        payout = self.get_object()
        next_status = (request.data.get("status") or "").strip().lower()
        if next_status not in TeacherPayout.Status.values:
            raise serializers.ValidationError(
                {"status": f"Must be one of: {', '.join(TeacherPayout.Status.values)}"}
            )
        old_status = payout.status
        payout.status = next_status
        payout.updated_by = request.user
        payout.save()
        log_event(
            user=request.user,
            category="teacher",
            action="update",
            summary=f"Changed payroll status for {payout.teacher.name} ({payout.month:%Y-%m}) from {old_status} to {next_status}",
            model_name="TeacherPayout",
            record_id=payout.pk,
            field_name="status",
            old_value=old_status,
            new_value=next_status,
        )
        return Response(self.get_serializer(payout).data)
