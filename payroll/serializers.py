from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers

from .models import CohortRateOverride, TeacherPayout, TeacherRateCard


class ValidatedModelSerializer(serializers.ModelSerializer):
    def save(self, **kwargs):
        try:
            with transaction.atomic():
                return super().save(**kwargs)
        except DjangoValidationError as exc:
            detail = (
                exc.message_dict
                if hasattr(exc, "message_dict")
                else {"non_field_errors": exc.messages}
            )
            raise serializers.ValidationError(detail) from exc
        except IntegrityError as exc:
            raise serializers.ValidationError(
                {"non_field_errors": ["A duplicate or conflicting payroll record exists."]}
            ) from exc


class CohortRateOverrideSerializer(ValidatedModelSerializer):
    class_label = serializers.CharField(source="class_obj.__str__", read_only=True)

    class Meta:
        model = CohortRateOverride
        fields = [
            "id",
            "rate_card",
            "class_obj",
            "class_label",
            "rate_per_session",
            "hourly_rate",
        ]
        read_only_fields = ["id", "class_label"]


class TeacherRateCardSerializer(ValidatedModelSerializer):
    teacher_name = serializers.CharField(source="teacher.name", read_only=True)
    teacher_code = serializers.CharField(source="teacher.unique_code", read_only=True)
    employment_type = serializers.CharField(source="teacher.employment_type", read_only=True)
    active_base_rate = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )
    cohort_overrides = CohortRateOverrideSerializer(many=True, read_only=True)

    class Meta:
        model = TeacherRateCard
        fields = [
            "id",
            "teacher",
            "teacher_name",
            "teacher_code",
            "employment_type",
            "rate_type",
            "rate_per_session",
            "hourly_rate",
            "active_base_rate",
            "currency",
            "notes",
            "cohort_overrides",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "teacher_name",
            "teacher_code",
            "employment_type",
            "active_base_rate",
            "cohort_overrides",
            "updated_at",
        ]


class TeacherPayoutSerializer(ValidatedModelSerializer):
    teacher_name = serializers.CharField(source="teacher.name", read_only=True)
    teacher_code = serializers.CharField(source="teacher.unique_code", read_only=True)
    employment_type = serializers.CharField(source="teacher.employment_type", read_only=True)
    currency = serializers.SerializerMethodField()
    has_rate_card = serializers.SerializerMethodField()
    cohort_overrides_count = serializers.SerializerMethodField()

    class Meta:
        model = TeacherPayout
        fields = [
            "id",
            "teacher",
            "teacher_name",
            "teacher_code",
            "employment_type",
            "currency",
            "has_rate_card",
            "cohort_overrides_count",
            "month",
            "period_start",
            "period_end",
            "rate_type",
            "base_rate",
            "sessions_count",
            "completed_sessions_count",
            "scheduled_sessions_count",
            "total_hours",
            "session_earnings",
            "bonus",
            "deduction",
            "adjustment_note",
            "net_payout",
            "status",
            "session_breakdown",
            "approved_at",
            "paid_at",
            "updated_by",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "teacher",
            "teacher_name",
            "teacher_code",
            "employment_type",
            "currency",
            "has_rate_card",
            "cohort_overrides_count",
            "month",
            "period_start",
            "period_end",
            "rate_type",
            "base_rate",
            "sessions_count",
            "completed_sessions_count",
            "scheduled_sessions_count",
            "total_hours",
            "session_earnings",
            "net_payout",
            "session_breakdown",
            "approved_at",
            "paid_at",
            "updated_by",
            "updated_at",
        ]

    def get_currency(self, obj):
        rate_card = getattr(obj.teacher, "rate_card", None)
        return rate_card.currency if rate_card else "MMK"

    def get_has_rate_card(self, obj):
        return getattr(obj.teacher, "rate_card", None) is not None

    def get_cohort_overrides_count(self, obj):
        rate_card = getattr(obj.teacher, "rate_card", None)
        if rate_card is None:
            return 0
        return rate_card.cohort_overrides.count()
