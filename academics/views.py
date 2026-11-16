from datetime import date, time
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from people.audit import AuditedViewSetMixin, log_event
from people.models import Student
from people.permissions import IsActiveAuthenticated, IsStaffOrAbove
from timetable.models import Class

from .models import (
    AcademicYear,
    Exam,
    ExamResult,
    Term,
    build_class_broadsheet,
    build_parent_portal_data,
    detect_exam_conflicts,
)
from .serializers import (
    AcademicYearSerializer,
    ExamResultSerializer,
    ExamSerializer,
    TermSerializer,
)


class AcademicConflict(APIException):
    status_code = 409
    default_detail = "Conflicting academic record. Refresh the data and retry."


class AcademicMixin:
    filter_fields = {}

    def get_queryset(self):
        qs = super().get_queryset()
        for parameter, lookup in self.filter_fields.items():
            value = self.request.query_params.get(parameter)
            if value is not None and value != "" and value != "all":
                try:
                    parsed = int(value)
                    if parsed < 1:
                        raise ValueError
                except (TypeError, ValueError):
                    raise ValidationError({parameter: "Must be a positive integer."})
                qs = qs.filter(**{lookup: parsed})
        return qs

    def handle_exception(self, exc):
        if isinstance(exc, DjangoValidationError):
            exc = ValidationError(
                exc.message_dict if hasattr(exc, "message_dict")
                else {"non_field_errors": exc.messages}
            )
        elif isinstance(exc, DjangoPermissionDenied):
            exc = PermissionDenied(str(exc))
        elif isinstance(exc, IntegrityError):
            exc = AcademicConflict()
        return super().handle_exception(exc)


class AcademicYearViewSet(AcademicMixin, AuditedViewSetMixin, viewsets.ModelViewSet):
    queryset = AcademicYear.objects.prefetch_related("terms").all()
    serializer_class = AcademicYearSerializer
    permission_classes = [IsStaffOrAbove]
    audit_category = "other"
    audit_noun = "academic year"
    pagination_class = None


class TermViewSet(AcademicMixin, AuditedViewSetMixin, viewsets.ModelViewSet):
    queryset = Term.objects.select_related("academic_year", "published_by").all()
    serializer_class = TermSerializer
    permission_classes = [IsStaffOrAbove]
    audit_category = "other"
    audit_noun = "exam term"
    filter_fields = {"academic_year_id": "academic_year_id"}
    http_method_names = ["get", "patch", "post", "head", "options"]
    pagination_class = None

    def get_queryset(self):
        qs = super().get_queryset()
        term_type = (self.request.query_params.get("term_type") or "").strip().lower()
        if term_type in ("midterm", "final"):
            qs = qs.filter(term_type=term_type)
        return qs

    def perform_update(self, serializer):
        is_pub = serializer.validated_data.get("is_published", serializer.instance.is_published)
        if is_pub and not serializer.instance.is_published:
            serializer.save(published_by=self.request.user, published_at=timezone.now())
        elif not is_pub:
            serializer.save(published_by=None, published_at=None)
        else:
            serializer.save()
        self._log_instance("update", serializer.instance)

    def _set_publish(self, request, term: Term, published: bool):
        term.is_published = published
        if published:
            term.published_by = request.user
            term.published_at = timezone.now()
        else:
            term.published_by = None
            term.published_at = None
        term.save()
        log_event(
            user=request.user,
            category="other",
            action="update",
            summary=f"{'Published' if published else 'Unpublished'} {term}",
            model_name="Term",
            record_id=term.pk,
            field_name="is_published",
            new_value=str(published),
        )
        return Response(self.get_serializer(term).data)

    @action(detail=True, methods=["post"])
    def toggle_publish(self, request, pk=None):
        term = self.get_object()
        return self._set_publish(request, term, not term.is_published)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        return self._set_publish(request, self.get_object(), True)

    @action(detail=True, methods=["post"])
    def unpublish(self, request, pk=None):
        return self._set_publish(request, self.get_object(), False)


class ExamViewSet(AcademicMixin, AuditedViewSetMixin, viewsets.ModelViewSet):
    queryset = (
        Exam.objects.select_related("term", "term__academic_year", "class_obj", "subject")
        .annotate(_results_count=Count("results"))
        .order_by("exam_date", "start_time", "class_obj_id", "subject_id")
    )
    serializer_class = ExamSerializer
    permission_classes = [IsStaffOrAbove]
    audit_category = "other"
    audit_noun = "exam"
    filter_fields = {
        "term_id": "term_id",
        "academic_year_id": "term__academic_year_id",
        "class_id": "class_obj_id",
        "subject_id": "subject_id",
    }

    def get_queryset(self):
        qs = super().get_queryset()
        term_type = (self.request.query_params.get("term_type") or "").strip().lower()
        if term_type in ("midterm", "final"):
            qs = qs.filter(term__term_type=term_type)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
        self._log_instance("create", serializer.instance)

    @action(detail=False, methods=["get"])
    def conflicts(self, request):
        raw_class = request.query_params.get("class_id") or request.query_params.get("class_obj")
        raw_date = request.query_params.get("exam_date")
        raw_start = request.query_params.get("start_time")
        raw_end = request.query_params.get("end_time")
        raw_room = request.query_params.get("room", "")
        raw_exclude = request.query_params.get("exclude_id")

        if not (raw_class and raw_date and raw_start and raw_end):
            return Response(
                {"error": "class_id, exam_date, start_time, and end_time are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            class_obj_id = int(raw_class)
            exam_date = date.fromisoformat(raw_date)
            start_time = time.fromisoformat(raw_start)
            end_time = time.fromisoformat(raw_end)
            exclude_id = int(raw_exclude) if raw_exclude else None
        except (TypeError, ValueError) as exc:
            return Response(
                {"error": f"Invalid conflict check parameter: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if end_time <= start_time:
            return Response(
                {"error": "end_time must be after start_time."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            detect_exam_conflicts(
                class_obj_id=class_obj_id,
                exam_date=exam_date,
                start_time=start_time,
                end_time=end_time,
                room=raw_room,
                exclude_exam_id=exclude_id,
            )
        )

    @action(detail=True, methods=["post"])
    def bulk_marks(self, request, pk=None):
        exam = self.get_object()
        records = request.data.get("records", request.data)
        if not isinstance(records, list):
            return Response(
                {"error": 'Expected {"records": [...]} or a list of mark entries.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        saved = _bulk_upsert_exam_results(
            records=records,
            actor=request.user,
            default_exam=exam,
        )
        return Response(
            {
                "exam_id": exam.pk,
                "saved_count": len(saved),
                "results": ExamResultSerializer(saved, many=True).data,
            },
            status=status.HTTP_200_OK,
        )


def _parse_decimal(raw_val, field_name: str) -> Decimal:
    try:
        return Decimal(str(raw_val))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError({field_name: f"Invalid decimal value: {raw_val}"})


def _bulk_upsert_exam_results(*, records: list, actor, default_exam: Exam | None = None) -> list[ExamResult]:
    saved: list[ExamResult] = []
    with transaction.atomic():
        for idx, item in enumerate(records):
            if not isinstance(item, dict):
                raise ValidationError({"records": f"Item {idx} must be an object."})
            exam_id = default_exam.pk if default_exam is not None else item.get("exam_id") or item.get("exam")
            student_id = item.get("student_id") or item.get("student")
            if not exam_id or not student_id:
                raise ValidationError({"records": f"Item {idx}: exam_id and student_id are required."})

            is_absent = bool(item.get("is_absent", False))
            raw_marks = item.get("marks_obtained", item.get("marks"))
            if raw_marks is None or raw_marks == "":
                if is_absent:
                    marks_val = Decimal("0.00")
                else:
                    raise ValidationError({"records": f"Item {idx}: marks_obtained is required unless absent."})
            else:
                marks_val = _parse_decimal(raw_marks, "marks_obtained")

            remarks = str(item.get("remarks", "") or "")
            existing = ExamResult.objects.filter(exam_id=exam_id, student_id=student_id).first()
            if existing:
                existing.marks_obtained = marks_val
                existing.is_absent = is_absent
                existing.remarks = remarks
                existing.recorded_by = actor
                existing.save()
                saved.append(existing)
            else:
                created = ExamResult.objects.create(
                    exam_id=exam_id,
                    student_id=student_id,
                    marks_obtained=marks_val,
                    is_absent=is_absent,
                    remarks=remarks,
                    recorded_by=actor,
                )
                saved.append(created)
    return saved


class ExamResultViewSet(AcademicMixin, AuditedViewSetMixin, viewsets.ModelViewSet):
    queryset = ExamResult.objects.select_related(
        "exam", "exam__term", "exam__class_obj", "exam__subject", "student"
    ).all()
    serializer_class = ExamResultSerializer
    permission_classes = [IsStaffOrAbove]
    audit_category = "other"
    audit_noun = "exam result"
    filter_fields = {
        "exam_id": "exam_id",
        "student_id": "student_id",
        "term_id": "exam__term_id",
        "class_id": "exam__class_obj_id",
        "subject_id": "exam__subject_id",
    }

    def perform_create(self, serializer):
        serializer.save(recorded_by=self.request.user)
        self._log_instance("create", serializer.instance)

    def perform_update(self, serializer):
        serializer.save(recorded_by=self.request.user)
        self._log_instance("update", serializer.instance)

    @action(detail=False, methods=["post"])
    def bulk_upsert(self, request):
        records = request.data.get("records", request.data)
        if not isinstance(records, list):
            return Response(
                {"error": 'Expected {"records": [...]} or a list of mark entries.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        saved = _bulk_upsert_exam_results(records=records, actor=request.user)
        return Response(
            {
                "saved_count": len(saved),
                "results": ExamResultSerializer(saved, many=True).data,
            },
            status=status.HTTP_200_OK,
        )


class BroadsheetAPIView(AcademicMixin, APIView):
    permission_classes = [IsStaffOrAbove]

    def get(self, request):
        raw_term_id = request.query_params.get("term_id")
        raw_class_id = request.query_params.get("class_id")
        if not raw_term_id or not raw_class_id:
            return Response(
                {"error": "term_id and class_id query parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            term_id = int(raw_term_id)
            class_id = int(raw_class_id)
        except (TypeError, ValueError):
            return Response(
                {"error": "term_id and class_id must be positive integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        term = get_object_or_404(Term.objects.select_related("academic_year"), pk=term_id)
        class_obj = get_object_or_404(Class, pk=class_id)
        return Response(build_class_broadsheet(term, class_obj))


class ParentPortalAPIView(AcademicMixin, APIView):
    permission_classes = [IsActiveAuthenticated]

    def get(self, request):
        raw_student = request.query_params.get("student_id")
        raw_year = request.query_params.get("academic_year_id")
        student_id = None
        academic_year_id = None

        if raw_student not in (None, "", "all"):
            try:
                student_id = int(raw_student)
            except (TypeError, ValueError):
                return Response(
                    {"error": "student_id must be a positive integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        if raw_year not in (None, "", "all"):
            try:
                academic_year_id = int(raw_year)
            except (TypeError, ValueError):
                return Response(
                    {"error": "academic_year_id must be a positive integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        data = build_parent_portal_data(
            request.user,
            student_id=student_id,
            academic_year_id=academic_year_id,
        )
        return Response(data)
