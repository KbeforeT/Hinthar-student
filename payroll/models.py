from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from class_sessions.datetime_bounds import aware_day_range, month_day_range

ZERO = Decimal("0.00")


def rounded_money(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class TeacherRateCard(models.Model):
    class RateType(models.TextChoices):
        PER_SESSION = "rate_per_session", "Rate per Session"
        HOURLY = "hourly_rate", "Hourly Rate"

    teacher = models.OneToOneField(
        "people.Teacher", on_delete=models.CASCADE, related_name="rate_card"
    )
    rate_type = models.CharField(
        max_length=24, choices=RateType.choices, default=RateType.PER_SESSION
    )
    rate_per_session = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    hourly_rate = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    currency = models.CharField(max_length=10, default="MMK")
    notes = models.CharField(max_length=500, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["teacher_id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(rate_per_session__gte=0, hourly_rate__gte=0),
                name="teacher_rate_nonnegative",
            ),
        ]

    def clean(self):
        if self.rate_per_session is None:
            self.rate_per_session = ZERO
        if self.hourly_rate is None:
            self.hourly_rate = ZERO
        if not (self.currency or "").strip():
            self.currency = "MMK"

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def active_base_rate(self) -> Decimal:
        if self.rate_type == self.RateType.HOURLY:
            return self.hourly_rate
        return self.rate_per_session

    def __str__(self):
        return f"{self.teacher.name} ({self.get_rate_type_display()}: {self.active_base_rate} {self.currency})"


class CohortRateOverride(models.Model):
    rate_card = models.ForeignKey(
        TeacherRateCard, on_delete=models.CASCADE, related_name="cohort_overrides"
    )
    class_obj = models.ForeignKey(
        "timetable.Class", on_delete=models.CASCADE, related_name="teacher_rate_overrides"
    )
    rate_per_session = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(ZERO)],
    )
    hourly_rate = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(ZERO)],
    )

    class Meta:
        ordering = ["rate_card_id", "class_obj_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["rate_card", "class_obj"],
                name="unique_teacher_cohort_rate_override",
            ),
        ]

    def clean(self):
        if self.rate_per_session is None and self.hourly_rate is None:
            raise ValidationError(
                "Provide at least one override amount (rate_per_session or hourly_rate)."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.rate_card.teacher.name} @ {self.class_obj}"


class TeacherPayout(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        PAID = "paid", "Paid"

    teacher = models.ForeignKey(
        "people.Teacher", on_delete=models.CASCADE, related_name="payouts"
    )
    month = models.DateField(help_text="Payroll month normalized to YYYY-MM-01.")
    period_start = models.DateField()
    period_end = models.DateField()
    rate_type = models.CharField(
        max_length=24,
        choices=TeacherRateCard.RateType.choices,
        default=TeacherRateCard.RateType.PER_SESSION,
    )
    base_rate = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    sessions_count = models.PositiveIntegerField(default=0)
    completed_sessions_count = models.PositiveIntegerField(default=0)
    scheduled_sessions_count = models.PositiveIntegerField(default=0)
    total_hours = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)
    session_earnings = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    bonus = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)]
    )
    deduction = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)]
    )
    adjustment_note = models.CharField(max_length=500, blank=True, default="")
    net_payout = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    session_breakdown = models.JSONField(default=dict, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_payouts",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-month", "teacher_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "month"], name="unique_teacher_payout_month"
            ),
            models.CheckConstraint(
                condition=Q(bonus__gte=0, deduction__gte=0, net_payout__gte=0),
                name="teacher_payout_amounts_nonnegative",
            ),
        ]

    def clean(self):
        if self.month:
            self.month = self.month.replace(day=1)
            if not self.period_start or not self.period_end:
                self.period_start, self.period_end = month_day_range(
                    self.month.year, self.month.month
                )
        if self.bonus is None:
            self.bonus = ZERO
        if self.deduction is None:
            self.deduction = ZERO
        if self.session_earnings is None:
            self.session_earnings = ZERO

        computed = rounded_money(self.session_earnings + self.bonus - self.deduction)
        self.net_payout = max(ZERO, computed)

        now = timezone.now()
        if self.status == self.Status.DRAFT:
            self.approved_at = None
            self.paid_at = None
        elif self.status == self.Status.APPROVED:
            if not self.approved_at:
                self.approved_at = now
            self.paid_at = None
        elif self.status == self.Status.PAID:
            if not self.approved_at:
                self.approved_at = now
            if not self.paid_at:
                self.paid_at = now

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.teacher.name} — {self.month:%Y-%m} ({self.get_status_display()})"


def _resolve_session_rate(
    rate_card: TeacherRateCard | None,
    overrides_by_class: dict[int, CohortRateOverride],
    class_obj_id: int | None,
) -> tuple[str, Decimal, bool]:
    if rate_card is None:
        return TeacherRateCard.RateType.PER_SESSION, ZERO, False

    rate_type = rate_card.rate_type
    override = overrides_by_class.get(class_obj_id) if class_obj_id else None

    if rate_type == TeacherRateCard.RateType.HOURLY:
        if override is not None and override.hourly_rate is not None:
            return rate_type, override.hourly_rate, True
        return rate_type, rate_card.hourly_rate, False

    if override is not None and override.rate_per_session is not None:
        return rate_type, override.rate_per_session, True
    return rate_type, rate_card.rate_per_session, False


def compute_teacher_period_breakdown(teacher, period_start: date, period_end: date) -> dict:
    """Compute session-level and cohort-grouped earnings for a teacher in [period_start, period_end]."""
    from class_sessions.models import AdHocSession, Session

    rate_card = (
        TeacherRateCard.objects.filter(teacher=teacher)
        .prefetch_related("cohort_overrides__class_obj")
        .first()
    )
    overrides_by_class: dict[int, CohortRateOverride] = {}
    if rate_card:
        for ov in rate_card.cohort_overrides.all():
            overrides_by_class[ov.class_obj_id] = ov

    rate_type = rate_card.rate_type if rate_card else TeacherRateCard.RateType.PER_SESSION
    base_rate = rate_card.active_base_rate if rate_card else ZERO
    currency = rate_card.currency if rate_card else "MMK"

    start_dt, end_dt = aware_day_range(period_start, period_end)
    teacher_filter = Q(actual_teacher=teacher) | Q(actual_teacher__isnull=True, teacher=teacher)

    regular_sessions = list(
        Session.objects.filter(
            start_time__gte=start_dt,
            start_time__lt=end_dt,
            status__in=("scheduled", "completed"),
        )
        .filter(teacher_filter)
        .select_related("class_obj", "timetable_slot__subject", "teacher", "actual_teacher")
        .order_by("start_time", "pk")
    )

    adhoc_sessions = list(
        AdHocSession.objects.filter(
            date__gte=period_start,
            date__lte=period_end,
            status__in=("scheduled", "completed"),
        )
        .filter(teacher_filter)
        .select_related("subject", "teacher", "actual_teacher")
        .order_by("date", "start_time", "pk")
    )

    session_items: list[dict] = []
    cohort_buckets: dict[tuple[int | None, str, str, bool], dict] = {}
    total_hours = ZERO
    session_earnings = ZERO
    completed_count = 0
    scheduled_count = 0

    for sess in regular_sessions:
        local_start = timezone.localtime(sess.start_time)
        local_end = timezone.localtime(sess.end_time)
        duration_hours = rounded_money(
            Decimal(str((sess.end_time - sess.start_time).total_seconds())) / Decimal("3600")
        )
        applied_type, rate_applied, is_override = _resolve_session_rate(
            rate_card, overrides_by_class, sess.class_obj_id
        )
        amount = (
            rounded_money(rate_applied * duration_hours)
            if applied_type == TeacherRateCard.RateType.HOURLY
            else rounded_money(rate_applied)
        )
        cohort_label = str(sess.class_obj) if sess.class_obj else "Unassigned"
        subject_name = (
            sess.timetable_slot.subject.name
            if sess.timetable_slot_id and sess.timetable_slot.subject_id
            else "Lesson"
        )
        is_substitute = bool(sess.actual_teacher_id == teacher.pk and sess.teacher_id != teacher.pk)

        if sess.status == "completed":
            completed_count += 1
        else:
            scheduled_count += 1
        total_hours += duration_hours
        session_earnings += amount

        session_items.append({
            "kind": "session",
            "session_id": sess.pk,
            "date": local_start.date().isoformat(),
            "start_time": local_start.strftime("%H:%M"),
            "end_time": local_end.strftime("%H:%M"),
            "duration_hours": str(duration_hours),
            "class_id": sess.class_obj_id,
            "cohort_label": cohort_label,
            "subject_name": subject_name,
            "status": sess.status,
            "is_substitute": is_substitute,
            "rate_type": applied_type,
            "rate_applied": str(rate_applied),
            "is_cohort_override": is_override,
            "amount": str(amount),
            "_sort_key": (local_start.date().isoformat(), local_start.strftime("%H:%M"), sess.pk),
        })

        bucket_key = (sess.class_obj_id, cohort_label, str(rate_applied), is_override)
        bucket = cohort_buckets.setdefault(
            bucket_key,
            {
                "class_id": sess.class_obj_id,
                "cohort_label": cohort_label,
                "sessions_count": 0,
                "completed_count": 0,
                "scheduled_count": 0,
                "total_hours": ZERO,
                "rate_type": applied_type,
                "rate_applied": str(rate_applied),
                "is_cohort_override": is_override,
                "subtotal": ZERO,
            },
        )
        bucket["sessions_count"] += 1
        if sess.status == "completed":
            bucket["completed_count"] += 1
        else:
            bucket["scheduled_count"] += 1
        bucket["total_hours"] += duration_hours
        bucket["subtotal"] += amount

    for adhoc in adhoc_sessions:
        start_dt_naive = datetime.combine(adhoc.date, adhoc.start_time)
        end_dt_naive = datetime.combine(adhoc.date, adhoc.end_time)
        duration_hours = rounded_money(
            Decimal(str((end_dt_naive - start_dt_naive).total_seconds())) / Decimal("3600")
        )
        applied_type, rate_applied, is_override = _resolve_session_rate(
            rate_card, overrides_by_class, None
        )
        amount = (
            rounded_money(rate_applied * duration_hours)
            if applied_type == TeacherRateCard.RateType.HOURLY
            else rounded_money(rate_applied)
        )
        is_substitute = bool(adhoc.actual_teacher_id == teacher.pk and adhoc.teacher_id != teacher.pk)

        if adhoc.status == "completed":
            completed_count += 1
        else:
            scheduled_count += 1
        total_hours += duration_hours
        session_earnings += amount

        session_items.append({
            "kind": "adhoc",
            "session_id": adhoc.pk,
            "date": adhoc.date.isoformat(),
            "start_time": adhoc.start_time.strftime("%H:%M"),
            "end_time": adhoc.end_time.strftime("%H:%M"),
            "duration_hours": str(duration_hours),
            "class_id": None,
            "cohort_label": "AD-HOC",
            "subject_name": adhoc.subject.name if adhoc.subject_id else "Ad-Hoc Tutoring",
            "status": adhoc.status,
            "is_substitute": is_substitute,
            "rate_type": applied_type,
            "rate_applied": str(rate_applied),
            "is_cohort_override": False,
            "amount": str(amount),
            "_sort_key": (adhoc.date.isoformat(), adhoc.start_time.strftime("%H:%M"), adhoc.pk),
        })

        bucket_key = (None, "AD-HOC", str(rate_applied), False)
        bucket = cohort_buckets.setdefault(
            bucket_key,
            {
                "class_id": None,
                "cohort_label": "AD-HOC",
                "sessions_count": 0,
                "completed_count": 0,
                "scheduled_count": 0,
                "total_hours": ZERO,
                "rate_type": applied_type,
                "rate_applied": str(rate_applied),
                "is_cohort_override": False,
                "subtotal": ZERO,
            },
        )
        bucket["sessions_count"] += 1
        if adhoc.status == "completed":
            bucket["completed_count"] += 1
        else:
            bucket["scheduled_count"] += 1
        bucket["total_hours"] += duration_hours
        bucket["subtotal"] += amount

    session_items.sort(key=lambda item: item["_sort_key"])
    for item in session_items:
        item.pop("_sort_key", None)

    cohorts_summary = []
    for bucket in sorted(cohort_buckets.values(), key=lambda b: b["cohort_label"]):
        cohorts_summary.append({
            **bucket,
            "total_hours": str(rounded_money(bucket["total_hours"])),
            "subtotal": str(rounded_money(bucket["subtotal"])),
        })

    return {
        "has_rate_card": rate_card is not None,
        "currency": currency,
        "rate_type": rate_type,
        "base_rate": rounded_money(base_rate),
        "sessions_count": len(session_items),
        "completed_sessions_count": completed_count,
        "scheduled_sessions_count": scheduled_count,
        "total_hours": rounded_money(total_hours),
        "session_earnings": rounded_money(session_earnings),
        "session_breakdown": {
            "sessions": session_items,
            "cohorts": cohorts_summary,
        },
    }


def sync_teacher_payout(
    teacher,
    month_date: date,
    *,
    actor=None,
    force_recalculate: bool = False,
) -> TeacherPayout:
    """Create or refresh a TeacherPayout record for the specified month."""
    month_start = month_date.replace(day=1)
    _, period_end = month_day_range(month_start.year, month_start.month)

    with transaction.atomic():
        payout = TeacherPayout.objects.filter(teacher=teacher, month=month_start).first()
        if payout is not None and payout.status != TeacherPayout.Status.DRAFT and not force_recalculate:
            return payout

        calc = compute_teacher_period_breakdown(teacher, month_start, period_end)
        if payout is None:
            payout = TeacherPayout(
                teacher=teacher,
                month=month_start,
                period_start=month_start,
                period_end=period_end,
            )

        payout.period_start = month_start
        payout.period_end = period_end
        payout.rate_type = calc["rate_type"]
        payout.base_rate = calc["base_rate"]
        payout.sessions_count = calc["sessions_count"]
        payout.completed_sessions_count = calc["completed_sessions_count"]
        payout.scheduled_sessions_count = calc["scheduled_sessions_count"]
        payout.total_hours = calc["total_hours"]
        payout.session_earnings = calc["session_earnings"]
        payout.session_breakdown = calc["session_breakdown"]
        if actor is not None:
            payout.updated_by = actor
        payout.save()
        return payout
