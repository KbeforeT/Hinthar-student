import re
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone

ACADEMIC_YEAR_RE = re.compile(r"^(\d{4})-(\d{4})$")
CANONICAL_STUDENT_CODE_RE = re.compile(r"^(?:HIS|SPD|SPN|YWM)\d{2}-\d{5}$")


def format_student_identifier(student) -> str:
    """Return canonical student identifier (e.g. SPD26-00043), normalizing legacy codes."""
    raw = (getattr(student, "unique_code", None) or "").strip()
    if CANONICAL_STUDENT_CODE_RE.match(raw):
        return raw

    trailing_digits = re.search(r"(\d+)\s*$", raw)
    seq = int(trailing_digits.group(1)) if trailing_digits else (getattr(student, "pk", None) or 1)
    enroll_date = getattr(student, "enrollment_date", None)
    year_two = (enroll_date.year % 100) if enroll_date else 26
    if raw:
        school_code = "SPD"
    else:
        raw_school = (getattr(student, "school_code", None) or "SPD").upper()
        school_code = raw_school if raw_school in ("HIS", "SPD", "SPN", "YWM") else "SPD"
    return f"{school_code}{year_two:02d}-{seq:05d}"


def grade_for(percentage):
    """Continuous bands: A* >= 90, A >= 80, B >= 70, C >= 60, D >= 50, F < 50."""
    for minimum, letter in ((90, "A*"), (80, "A"), (70, "B"), (60, "C"), (50, "D")):
        if percentage >= minimum:
            return letter
    return "F"


def rounded(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class ValidatedModel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class AcademicYear(ValidatedModel):
    name = models.CharField(max_length=30, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-start_date", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__gt=F("start_date")),
                name="academic_year_dates_valid",
            ),
        ]

    def clean(self):
        self.name = (self.name or "").strip()
        match = ACADEMIC_YEAR_RE.match(self.name)
        if not match:
            raise ValidationError(
                {
                    "name": (
                        "Academic year label must follow YYYY-YYYY format "
                        "(e.g. 2026-2027)."
                    )
                }
            )

        start_year = int(match.group(1))
        end_year = int(match.group(2))
        if end_year <= start_year:
            raise ValidationError(
                {
                    "name": (
                        f"Invalid academic year range '{self.name}': "
                        f"end year ({end_year}) must be after start year ({start_year}), "
                        f"e.g. {start_year}-{start_year + 1}."
                    )
                }
            )
        if end_year != start_year + 1:
            raise ValidationError(
                {
                    "name": (
                        f"Academic year label '{self.name}' must span two consecutive years "
                        f"(expected {start_year}-{start_year + 1})."
                    )
                }
            )

        if self.start_date and self.end_date and self.end_date <= self.start_date:
            raise ValidationError({"end_date": "Must be after start_date."})
        if self.start_date and self.start_date.year != start_year:
            raise ValidationError(
                {
                    "start_date": (
                        f"Start date year ({self.start_date.year}) must match "
                        f"academic year label '{self.name}' ({start_year})."
                    )
                }
            )
        if self.end_date and self.end_date.year != end_year:
            raise ValidationError(
                {
                    "end_date": (
                        f"End date year ({self.end_date.year}) must match "
                        f"academic year label '{self.name}' ({end_year})."
                    )
                }
            )

        if self.start_date and self.end_date:
            conflict = (
                AcademicYear.objects.filter(
                    start_date__lte=self.end_date,
                    end_date__gte=self.start_date,
                )
                .exclude(pk=self.pk)
                .first()
            )
            if conflict is not None:
                raise ValidationError(
                    {
                        "non_field_errors": (
                            f"Date range conflict: {self.start_date} to {self.end_date} "
                            f"overlaps with existing Academic Year {conflict.name} "
                            f"({conflict.start_date} to {conflict.end_date})."
                        )
                    }
                )

    def save(self, *args, **kwargs):
        # Automatically provision the two fixed exam terms: Midterm & Final Term.
        with transaction.atomic():
            super().save(*args, **kwargs)
            Term.objects.get_or_create(
                academic_year=self,
                number=1,
                defaults={"term_type": Term.TermType.MIDTERM},
            )
            Term.objects.get_or_create(
                academic_year=self,
                number=2,
                defaults={"term_type": Term.TermType.FINAL},
            )

    def __str__(self):
        return self.name


class Term(ValidatedModel):
    class TermType(models.TextChoices):
        MIDTERM = "midterm", "Midterm"
        FINAL = "final", "Final Term"

    academic_year = models.ForeignKey(
        AcademicYear, on_delete=models.CASCADE, related_name="terms"
    )
    number = models.PositiveSmallIntegerField(
        choices=[(1, "Midterm"), (2, "Final Term")]
    )
    term_type = models.CharField(
        max_length=16, choices=TermType.choices, default=TermType.MIDTERM
    )
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="published_terms",
    )

    class Meta:
        ordering = ["academic_year_id", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "number"], name="unique_year_term"
            ),
            models.UniqueConstraint(
                fields=["academic_year", "term_type"], name="unique_year_term_type"
            ),
            models.CheckConstraint(
                condition=Q(number__in=[1, 2]), name="term_one_or_two"
            ),
            models.CheckConstraint(
                condition=Q(term_type__in=["midterm", "final"]),
                name="valid_term_type",
            ),
        ]

    @property
    def name(self) -> str:
        return "Midterm" if self.number == 1 or self.term_type == self.TermType.MIDTERM else "Final Term"

    def clean(self):
        if self.number not in (1, 2):
            raise ValidationError({"number": "Only fixed terms 1 (Midterm) and 2 (Final Term) are allowed."})
        expected_type = self.TermType.MIDTERM if self.number == 1 else self.TermType.FINAL
        self.term_type = expected_type

    def save(self, *args, **kwargs):
        if self.is_published and not self.published_at:
            self.published_at = timezone.now()
        elif not self.is_published:
            self.published_at = None
        super().save(*args, **kwargs)

    def __str__(self):
        yr = self.academic_year.name if self.academic_year_id else "Unassigned"
        return f"{yr} — {self.name}"


class Exam(ValidatedModel):
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name="exams")
    class_obj = models.ForeignKey(
        "timetable.Class", on_delete=models.CASCADE, related_name="exams"
    )
    subject = models.ForeignKey(
        "people.Subject", on_delete=models.PROTECT, related_name="exams"
    )
    title = models.CharField(max_length=150, blank=True, default="")
    exam_date = models.DateField(db_index=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    room = models.CharField(max_length=100, blank=True, default="")
    max_marks = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("100.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    pass_marks = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("50.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_exams",
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["exam_date", "start_time", "class_obj_id", "subject_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["term", "class_obj", "subject"],
                name="unique_exam_per_term_class_subject",
            ),
            models.CheckConstraint(
                condition=Q(max_marks__gt=0),
                name="exam_positive_max_marks",
            ),
            models.CheckConstraint(
                condition=Q(pass_marks__gte=0) & Q(pass_marks__lte=F("max_marks")),
                name="exam_valid_pass_marks",
            ),
        ]
        indexes = [
            models.Index(fields=["term", "class_obj"], name="idx_exam_term_class"),
            models.Index(fields=["class_obj", "exam_date"], name="idx_exam_class_date"),
        ]

    def clean(self):
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError({"end_time": "end_time must be after start_time."})
        if self.max_marks is not None and self.pass_marks is not None:
            if self.pass_marks > self.max_marks:
                raise ValidationError({"pass_marks": "Pass marks cannot exceed max marks."})

        if self.term_id and self.exam_date:
            academic_year = self.term.academic_year
            if (
                self.exam_date < academic_year.start_date
                or self.exam_date > academic_year.end_date
            ):
                raise ValidationError(
                    {
                        "exam_date": (
                            f"Exam date ({self.exam_date}) must fall within Academic Year "
                            f"{academic_year.name} ({academic_year.start_date} to {academic_year.end_date})."
                        )
                    }
                )

        if self.subject_id and self.term_id and not (self.title or "").strip():
            self.title = f"{self.subject.name} — {self.term.name}"

        if self.term_id and self.class_obj_id and self.subject_id:
            dup = (
                Exam.objects.filter(
                    term_id=self.term_id,
                    class_obj_id=self.class_obj_id,
                    subject_id=self.subject_id,
                )
                .exclude(pk=self.pk)
                .select_related("subject", "term")
                .first()
            )
            if dup is not None:
                raise ValidationError(
                    {
                        "non_field_errors": (
                            f"Subject conflict: {self.class_obj} already has "
                            f"{dup.subject.name} scheduled in {dup.term.name} on {dup.exam_date}."
                        )
                    }
                )

        if self.class_obj_id and self.exam_date and self.start_time and self.end_time:
            overlapping = (
                Exam.objects.filter(
                    class_obj_id=self.class_obj_id,
                    exam_date=self.exam_date,
                    start_time__lt=self.end_time,
                    end_time__gt=self.start_time,
                )
                .exclude(pk=self.pk)
                .select_related("subject", "term")
            )
            conflict = overlapping.first()
            if conflict is not None:
                raise ValidationError(
                    {
                        "non_field_errors": (
                            f"Cohort exam overlap on {self.exam_date}: "
                            f"{self.class_obj} already has {conflict.subject.name} "
                            f"({conflict.term.name}) scheduled from "
                            f"{conflict.start_time:%H:%M} to {conflict.end_time:%H:%M}."
                        )
                    }
                )

    def __str__(self):
        subject_label = self.subject.name if self.subject_id else "Subject"
        cohort_label = str(self.class_obj) if self.class_obj_id else "Cohort"
        term_label = self.term.name if self.term_id else "Term"
        date_label = self.exam_date.isoformat() if self.exam_date else "TBA"
        return f"{subject_label} — {cohort_label} ({term_label}, {date_label})"


ExamSchedule = Exam


class ExamResult(ValidatedModel):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="results")
    student = models.ForeignKey(
        "people.Student", on_delete=models.CASCADE, related_name="exam_results"
    )
    marks_obtained = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    is_absent = models.BooleanField(default=False)
    remarks = models.CharField(max_length=500, blank=True, default="")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_exam_results",
        editable=False,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["exam_id", "student_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["exam", "student"],
                name="unique_exam_student_result",
            ),
            models.CheckConstraint(
                condition=Q(marks_obtained__gte=0),
                name="exam_result_nonnegative_marks",
            ),
        ]

    @property
    def marks(self):
        return self.marks_obtained

    @property
    def percentage(self) -> Decimal:
        if self.is_absent or not self.exam_id or not self.exam.max_marks:
            return Decimal("0.00")
        return rounded((self.marks_obtained / self.exam.max_marks) * Decimal("100"))

    @property
    def grade(self) -> str:
        return grade_for(self.percentage)

    @property
    def passed(self) -> bool:
        if self.is_absent:
            return False
        return self.marks_obtained >= self.exam.pass_marks

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"

    def clean(self):
        from timetable.models import ClassStudent, StudentSubjectEnrollment

        if self.is_absent and self.marks_obtained is None:
            self.marks_obtained = Decimal("0.00")

        if not self.exam_id or not self.student_id:
            return

        exam = Exam.objects.select_related("class_obj", "subject").get(pk=self.exam_id)
        if self.marks_obtained is not None and self.marks_obtained > exam.max_marks:
            raise ValidationError(
                {"marks_obtained": f"Marks cannot exceed exam maximum ({exam.max_marks})."}
            )

        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if old.student_id != self.student_id or old.exam_id != self.exam_id:
                raise ValidationError("Student and exam cannot change after creation.")
        else:
            is_primary = ClassStudent.objects.filter(
                student_id=self.student_id, class_obj_id=exam.class_obj_id
            ).exists()
            sse = StudentSubjectEnrollment.objects.filter(
                student_id=self.student_id,
                class_obj_id=exam.class_obj_id,
                subject_id=exam.subject_id,
            ).first()
            enrolled = (is_primary and (sse is None or sse.status != "dropped")) or (
                sse is not None and sse.status == "enrolled"
            )
            if not enrolled:
                raise ValidationError(
                    {"student": "Student is not enrolled in this cohort/subject."}
                )

    def __str__(self):
        if self.student_id and self.exam_id:
            code = format_student_identifier(self.student)
            score_str = "Absent" if self.is_absent else f"{self.marks_obtained}/{self.exam.max_marks}"
            return (
                f"{self.student.name} ({code}) — "
                f"{self.exam.subject.name} ({self.exam.term.name}): {score_str}"
            )
        return f"Exam Result #{self.pk or 'new'}"


def detect_exam_conflicts(
    *,
    class_obj_id: int,
    exam_date,
    start_time,
    end_time,
    room: str = "",
    exclude_exam_id: int | None = None,
    term_id: int | None = None,
    subject_id: int | None = None,
) -> dict:
    """Return cohort and optional room time-overlap warnings/conflicts for a candidate exam."""
    base_qs = Exam.objects.filter(
        exam_date=exam_date,
        start_time__lt=end_time,
        end_time__gt=start_time,
    ).select_related("class_obj", "subject", "term", "term__academic_year")
    if exclude_exam_id is not None:
        base_qs = base_qs.exclude(pk=exclude_exam_id)

    cohort_conflicts = []
    if term_id is not None:
        term_obj = Term.objects.select_related("academic_year").filter(pk=term_id).first()
        if term_obj is not None:
            ay = term_obj.academic_year
            if exam_date < ay.start_date or exam_date > ay.end_date:
                cohort_conflicts.append({
                    "id": -term_obj.pk,
                    "class_obj": class_obj_id,
                    "class_label": "",
                    "subject_id": subject_id or 0,
                    "subject_name": "",
                    "term_id": term_obj.pk,
                    "term_name": term_obj.name,
                    "exam_date": exam_date.isoformat(),
                    "start_time": start_time.strftime("%H:%M"),
                    "end_time": end_time.strftime("%H:%M"),
                    "room": room,
                    "message": (
                        f"Date conflict: Exam date {exam_date} is outside Academic Year "
                        f"{ay.name} ({ay.start_date} to {ay.end_date})."
                    ),
                })
            if subject_id is not None:
                dup_qs = Exam.objects.filter(
                    term_id=term_id,
                    class_obj_id=class_obj_id,
                    subject_id=subject_id,
                ).select_related("class_obj", "subject", "term")
                if exclude_exam_id is not None:
                    dup_qs = dup_qs.exclude(pk=exclude_exam_id)
                dup = dup_qs.first()
                if dup is not None:
                    cohort_conflicts.append({
                        "id": dup.pk,
                        "class_obj": dup.class_obj_id,
                        "class_label": str(dup.class_obj),
                        "subject_id": dup.subject_id,
                        "subject_name": dup.subject.name,
                        "term_id": dup.term_id,
                        "term_name": dup.term.name,
                        "exam_date": dup.exam_date.isoformat(),
                        "start_time": dup.start_time.strftime("%H:%M"),
                        "end_time": dup.end_time.strftime("%H:%M"),
                        "room": dup.room,
                        "message": (
                            f"Subject conflict: {dup.class_obj} already has {dup.subject.name} "
                            f"scheduled in {dup.term.name} on {dup.exam_date} "
                            f"({dup.start_time:%H:%M}–{dup.end_time:%H:%M})."
                        ),
                    })

    for conflict in base_qs.filter(class_obj_id=class_obj_id):
        cohort_conflicts.append({
            "id": conflict.pk,
            "class_obj": conflict.class_obj_id,
            "class_label": str(conflict.class_obj),
            "subject_id": conflict.subject_id,
            "subject_name": conflict.subject.name,
            "term_id": conflict.term_id,
            "term_name": conflict.term.name,
            "exam_date": conflict.exam_date.isoformat(),
            "start_time": conflict.start_time.strftime("%H:%M"),
            "end_time": conflict.end_time.strftime("%H:%M"),
            "room": conflict.room,
            "message": (
                f"Cohort conflict: {conflict.class_obj} already has {conflict.subject.name} "
                f"({conflict.term.name}) from {conflict.start_time:%H:%M} to {conflict.end_time:%H:%M}."
            ),
        })

    room_conflicts = []
    cleaned_room = (room or "").strip()
    if cleaned_room:
        for conflict in base_qs.filter(room__iexact=cleaned_room).exclude(class_obj_id=class_obj_id):
            room_conflicts.append({
                "id": conflict.pk,
                "class_obj": conflict.class_obj_id,
                "class_label": str(conflict.class_obj),
                "subject_name": conflict.subject.name,
                "term_name": conflict.term.name,
                "exam_date": conflict.exam_date.isoformat(),
                "start_time": conflict.start_time.strftime("%H:%M"),
                "end_time": conflict.end_time.strftime("%H:%M"),
                "room": conflict.room,
                "message": (
                    f"Room notice: Room {conflict.room} is also booked by {conflict.class_obj} "
                    f"({conflict.subject.name}) from {conflict.start_time:%H:%M} to {conflict.end_time:%H:%M}."
                ),
            })

    return {
        "has_conflicts": len(cohort_conflicts) > 0,
        "has_room_warnings": len(room_conflicts) > 0,
        "cohort_conflicts": cohort_conflicts,
        "room_conflicts": room_conflicts,
    }


def _serialize_exam_cell(exam: Exam, result: ExamResult | None) -> dict:
    if result is None:
        return {
            "exam_id": exam.pk,
            "subject_id": exam.subject_id,
            "subject_name": exam.subject.name,
            "result_id": None,
            "marks_obtained": None,
            "max_marks": str(exam.max_marks),
            "pass_marks": str(exam.pass_marks),
            "is_absent": False,
            "percentage": None,
            "grade": None,
            "passed": None,
            "status": "PENDING",
            "remarks": "",
        }
    return {
        "exam_id": exam.pk,
        "subject_id": exam.subject_id,
        "subject_name": exam.subject.name,
        "result_id": result.pk,
        "marks_obtained": str(result.marks_obtained),
        "max_marks": str(exam.max_marks),
        "pass_marks": str(exam.pass_marks),
        "is_absent": result.is_absent,
        "percentage": str(result.percentage),
        "grade": result.grade,
        "passed": result.passed,
        "status": result.status,
        "remarks": result.remarks,
    }


def build_class_broadsheet(term: Term, class_obj) -> dict:
    """Compute a complete cohort exam broadsheet with totals, average %, grades, pass/fail, and rank."""
    from timetable.conflicts import get_enrolled_students_for_course

    exams = list(
        Exam.objects.filter(term=term, class_obj=class_obj)
        .select_related("subject", "term", "class_obj")
        .order_by("exam_date", "start_time", "subject__name", "pk")
    )
    exam_ids = [e.pk for e in exams]

    students = get_enrolled_students_for_course(class_obj.pk)
    # Also include any student with recorded ExamResult for this cohort+term even if later transferred
    student_map = {s.pk: s for s in students}
    results = list(
        ExamResult.objects.filter(exam_id__in=exam_ids)
        .select_related("student", "exam", "exam__subject")
    )
    result_lookup: dict[tuple[int, int], ExamResult] = {}
    for res in results:
        result_lookup[(res.exam_id, res.student_id)] = res
        if res.student_id not in student_map:
            student_map[res.student_id] = res.student

    ordered_students = sorted(
        student_map.values(),
        key=lambda s: (format_student_identifier(s), (s.name or "").lower(), s.pk),
    )

    exam_result_counts: dict[int, int] = {eid: 0 for eid in exam_ids}
    for res in results:
        exam_result_counts[res.exam_id] = exam_result_counts.get(res.exam_id, 0) + 1

    student_rows = []
    for student in ordered_students:
        marks_by_exam: dict[str, dict] = {}
        marks_by_subject: dict[str, dict] = {}
        total_obtained = Decimal("0.00")
        total_max = Decimal("0.00")
        exams_taken = 0
        all_passed = True

        for exam in exams:
            res = result_lookup.get((exam.pk, student.pk))
            cell = _serialize_exam_cell(exam, res)
            marks_by_exam[str(exam.pk)] = cell
            marks_by_subject[str(exam.subject_id)] = cell
            if res is not None:
                exams_taken += 1
                total_obtained += res.marks_obtained
                total_max += exam.max_marks
                if not res.passed:
                    all_passed = False

        if exams_taken > 0 and total_max > 0:
            avg_pct = rounded((total_obtained / total_max) * Decimal("100"))
            letter = grade_for(avg_pct)
            pf_status = "PASS" if (all_passed and avg_pct >= Decimal("50.00")) else "FAIL"
        else:
            avg_pct = None
            letter = None
            pf_status = "PENDING"

        student_code = format_student_identifier(student)
        student_rows.append({
            "student_id": student.pk,
            "unique_code": student_code,
            "student_code": student_code,
            "name": student.name,
            "exam_candidate_number": student.exam_candidate_number,
            "marks_by_exam": marks_by_exam,
            "marks_by_subject": marks_by_subject,
            "total_marks_obtained": str(rounded(total_obtained)) if exams_taken > 0 else None,
            "total_max_marks": str(rounded(total_max)) if exams_taken > 0 else None,
            "exams_taken": exams_taken,
            "exams_total": len(exams),
            "average_percentage": str(avg_pct) if avg_pct is not None else None,
            "letter_grade": letter,
            "pass_fail_status": pf_status,
            "rank": None,
            "_sort_avg": avg_pct,
            "_sort_total": total_obtained,
        })

    # Assign competition rank (1, 2, 2, 4...) to graded students.
    graded_rows = [r for r in student_rows if r["_sort_avg"] is not None]
    graded_rows.sort(key=lambda r: (-r["_sort_avg"], -r["_sort_total"], r["name"].lower()))
    prev_score = None
    prev_rank = 0
    for idx, row in enumerate(graded_rows, start=1):
        score_key = (row["_sort_avg"], row["_sort_total"])
        if score_key != prev_score:
            prev_rank = idx
            prev_score = score_key
        row["rank"] = prev_rank

    for row in student_rows:
        row.pop("_sort_avg", None)
        row.pop("_sort_total", None)

    pass_count = sum(1 for r in student_rows if r["pass_fail_status"] == "PASS")
    fail_count = sum(1 for r in student_rows if r["pass_fail_status"] == "FAIL")
    pending_count = sum(1 for r in student_rows if r["pass_fail_status"] == "PENDING")
    graded_count = pass_count + fail_count
    class_avg = (
        str(rounded(sum((Decimal(r["average_percentage"]) for r in graded_rows), Decimal("0")) / graded_count))
        if graded_count > 0
        else None
    )
    pass_rate = (
        str(rounded((Decimal(pass_count) / Decimal(graded_count)) * Decimal("100")))
        if graded_count > 0
        else None
    )

    return {
        "term": {
            "id": term.pk,
            "academic_year_id": term.academic_year_id,
            "academic_year_name": term.academic_year.name,
            "number": term.number,
            "term_type": term.term_type,
            "name": term.name,
            "is_published": term.is_published,
            "published_at": term.published_at.isoformat() if term.published_at else None,
        },
        "class_obj": {
            "id": class_obj.pk,
            "label": str(class_obj),
            "education_level": class_obj.education_level,
            "cohort_identifier": class_obj.cohort_identifier,
            "cohort_sub_category": class_obj.cohort_sub_category,
        },
        "exams": [
            {
                "id": e.pk,
                "subject_id": e.subject_id,
                "subject_name": e.subject.name,
                "title": e.title or f"{e.subject.name} — {term.name}",
                "exam_date": e.exam_date.isoformat(),
                "start_time": e.start_time.strftime("%H:%M"),
                "end_time": e.end_time.strftime("%H:%M"),
                "room": e.room,
                "max_marks": str(e.max_marks),
                "pass_marks": str(e.pass_marks),
                "results_count": exam_result_counts.get(e.pk, 0),
            }
            for e in exams
        ],
        "students": student_rows,
        "summary": {
            "enrolled_count": len(student_rows),
            "exams_scheduled_count": len(exams),
            "graded_students_count": graded_count,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "pending_count": pending_count,
            "pass_rate": pass_rate,
            "class_average_percentage": class_avg,
        },
    }


def build_parent_portal_data(
    user,
    *,
    student_id: int | None = None,
    academic_year_id: int | None = None,
) -> dict:
    """Build scoped Parent Results Portal payload with Midterm vs Final comparison and schedules."""
    from django.core.exceptions import PermissionDenied
    from people.models import Student
    from people.roles import is_staff_or_above
    from timetable.conflicts import get_student_active_course_pairs
    from timetable.models import Class, ClassStudent

    staff_viewer = is_staff_or_above(user)
    if staff_viewer:
        accessible_qs = Student.objects.prefetch_related("enrolled_classes__class_obj").order_by("unique_code", "name", "pk")
    elif getattr(user, "role", None) == "parent":
        accessible_qs = Student.objects.filter(parent=user).prefetch_related("enrolled_classes__class_obj").order_by("unique_code", "name", "pk")
    elif getattr(user, "role", None) == "student":
        accessible_qs = Student.objects.filter(user=user).prefetch_related("enrolled_classes__class_obj").order_by("pk")
    else:
        raise PermissionDenied("Parent, student, or staff role required.")

    accessible_students = list(accessible_qs)
    if student_id is not None:
        selected_student = next((s for s in accessible_students if s.pk == student_id), None)
        if selected_student is None:
            if staff_viewer:
                raise ValidationError({"student_id": "Student not found."})
            raise PermissionDenied("You do not have access to this student's results.")
    else:
        selected_student = accessible_students[0] if accessible_students else None

    years = list(AcademicYear.objects.prefetch_related("terms").order_by("-start_date", "-pk"))
    if academic_year_id is not None:
        selected_year = next((y for y in years if y.pk == academic_year_id), None)
        if selected_year is None:
            raise ValidationError({"academic_year_id": "Academic year not found."})
    else:
        selected_year = next((y for y in years if y.is_active), years[0] if years else None)

    def _serialize_student_ref(s):
        class_labels = [str(e.class_obj) for e in s.enrolled_classes.all() if e.class_obj]
        code = format_student_identifier(s)
        return {
            "id": s.pk,
            "unique_code": code,
            "student_code": code,
            "name": s.name,
            "exam_candidate_number": s.exam_candidate_number,
            "class_labels": class_labels,
        }

    years_payload = [
        {
            "id": y.pk,
            "name": y.name,
            "start_date": y.start_date.isoformat(),
            "end_date": y.end_date.isoformat(),
            "is_active": y.is_active,
            "terms": [
                {
                    "id": t.pk,
                    "number": t.number,
                    "term_type": t.term_type,
                    "name": t.name,
                    "is_published": t.is_published,
                    "published_at": t.published_at.isoformat() if t.published_at else None,
                }
                for t in sorted(y.terms.all(), key=lambda x: x.number)
            ],
        }
        for y in years
    ]

    if selected_student is None or selected_year is None:
        return {
            "is_admin_preview": staff_viewer,
            "accessible_students": [_serialize_student_ref(s) for s in accessible_students],
            "student": _serialize_student_ref(selected_student) if selected_student else None,
            "academic_years": years_payload,
            "selected_academic_year": next((yp for yp in years_payload if selected_year and yp["id"] == selected_year.pk), None),
            "enrolled_subjects": [],
            "exam_schedule": [],
            "results_comparison": [],
            "term_summaries": {"midterm": None, "final_term": None},
        }

    active_pairs = get_student_active_course_pairs(selected_student.pk)
    # Include any cohort/subject for which the student has an existing exam result in this academic year
    student_year_results = list(
        ExamResult.objects.filter(
            student=selected_student,
            exam__term__academic_year=selected_year,
        ).select_related("exam", "exam__term", "exam__class_obj", "exam__subject")
    )
    for res in student_year_results:
        active_pairs.add((res.exam.class_obj_id, res.exam.subject_id))

    # Also if student has a primary cohort but no courses configured yet, include scheduled exams for their primary cohort
    primary_class_ids = set(
        ClassStudent.objects.filter(student=selected_student).values_list("class_obj_id", flat=True)
    )

    year_terms = {t.number: t for t in selected_year.terms.all()}
    midterm_term = year_terms.get(1)
    final_term = year_terms.get(2)

    # Fetch exams for this academic year across the student's enrolled (class_id, subject_id) or primary cohort
    exam_filter = Q()
    for cid, sid in active_pairs:
        exam_filter |= Q(class_obj_id=cid, subject_id=sid)
    if primary_class_ids:
        from timetable.models import StudentSubjectEnrollment
        dropped_pairs = set(
            StudentSubjectEnrollment.objects.filter(
                student=selected_student, status="dropped"
            ).values_list("class_obj_id", "subject_id")
        )
        exams_qs = Exam.objects.filter(term__academic_year=selected_year).filter(
            exam_filter | Q(class_obj_id__in=primary_class_ids)
        ).select_related("term", "class_obj", "subject").order_by("exam_date", "start_time", "pk")
        exams = [
            e for e in exams_qs
            if (e.class_obj_id, e.subject_id) not in dropped_pairs
        ]
    elif active_pairs:
        exams = list(
            Exam.objects.filter(term__academic_year=selected_year)
            .filter(exam_filter)
            .select_related("term", "class_obj", "subject")
            .order_by("exam_date", "start_time", "pk")
        )
    else:
        exams = []

    result_by_exam = {r.exam_id: r for r in student_year_results}

    # Build enrolled subject map from active_pairs + exams
    from people.models import Subject
    class_map = {c.pk: c for c in Class.objects.filter(pk__in={cid for cid, _ in active_pairs} | {e.class_obj_id for e in exams})}
    subject_map = {s.pk: s for s in Subject.objects.filter(pk__in={sid for _, sid in active_pairs} | {e.subject_id for e in exams})}

    subject_entries: dict[tuple[int, int], dict] = {}
    for cid, sid in active_pairs:
        cls = class_map.get(cid)
        subj = subject_map.get(sid)
        if cls and subj:
            subject_entries[(cid, sid)] = {
                "class_id": cid,
                "cohort_label": str(cls),
                "subject_id": sid,
                "subject_name": subj.name,
            }
    for e in exams:
        if (e.class_obj_id, e.subject_id) not in subject_entries:
            subject_entries[(e.class_obj_id, e.subject_id)] = {
                "class_id": e.class_obj_id,
                "cohort_label": str(e.class_obj),
                "subject_id": e.subject_id,
                "subject_name": e.subject.name,
            }

    enrolled_subjects = sorted(
        subject_entries.values(),
        key=lambda x: (x["subject_name"].lower(), x["cohort_label"]),
    )

    exam_schedule = [
        {
            "id": e.pk,
            "term_id": e.term_id,
            "term_number": e.term.number,
            "term_name": e.term.name,
            "term_type": e.term.term_type,
            "is_published": e.term.is_published,
            "class_id": e.class_obj_id,
            "cohort_label": str(e.class_obj),
            "subject_id": e.subject_id,
            "subject_name": e.subject.name,
            "title": e.title or f"{e.subject.name} — {e.term.name}",
            "exam_date": e.exam_date.isoformat(),
            "start_time": e.start_time.strftime("%H:%M"),
            "end_time": e.end_time.strftime("%H:%M"),
            "room": e.room,
            "max_marks": str(e.max_marks),
            "pass_marks": str(e.pass_marks),
        }
        for e in exams
    ]

    exam_by_key = {(e.term.number, e.class_obj_id, e.subject_id): e for e in exams}

    def _portal_term_cell(term_obj: Term | None, cid: int, sid: int) -> dict | None:
        if term_obj is None:
            return None
        exam_obj = exam_by_key.get((term_obj.number, cid, sid))
        if exam_obj is None:
            return None
        visible = term_obj.is_published or staff_viewer
        res = result_by_exam.get(exam_obj.pk) if visible else None
        if not visible:
            return {
                "exam_id": exam_obj.pk,
                "term_id": term_obj.pk,
                "is_published": False,
                "exam_date": exam_obj.exam_date.isoformat(),
                "max_marks": str(exam_obj.max_marks),
                "pass_marks": str(exam_obj.pass_marks),
                "marks_obtained": None,
                "is_absent": False,
                "percentage": None,
                "grade": None,
                "passed": None,
                "status": "UNPUBLISHED",
                "remarks": "",
            }
        cell = _serialize_exam_cell(exam_obj, res)
        cell["term_id"] = term_obj.pk
        cell["is_published"] = term_obj.is_published
        cell["exam_date"] = exam_obj.exam_date.isoformat()
        return cell

    results_comparison = []
    for entry in enrolled_subjects:
        cid = entry["class_id"]
        sid = entry["subject_id"]
        mid_cell = _portal_term_cell(midterm_term, cid, sid)
        fin_cell = _portal_term_cell(final_term, cid, sid)

        delta_pct = None
        if (
            mid_cell
            and fin_cell
            and mid_cell.get("percentage") is not None
            and fin_cell.get("percentage") is not None
        ):
            delta_pct = str(
                rounded(Decimal(fin_cell["percentage"]) - Decimal(mid_cell["percentage"]))
            )

        results_comparison.append({
            "class_id": cid,
            "cohort_label": entry["cohort_label"],
            "subject_id": sid,
            "subject_name": entry["subject_name"],
            "midterm": mid_cell,
            "final_term": fin_cell,
            "delta_percentage": delta_pct,
        })

    def _build_term_summary(term_obj: Term | None) -> dict | None:
        if term_obj is None:
            return None
        visible = term_obj.is_published or staff_viewer
        term_exams = [e for e in exams if e.term_id == term_obj.pk]
        if not visible:
            return {
                "term_id": term_obj.pk,
                "name": term_obj.name,
                "term_type": term_obj.term_type,
                "is_published": False,
                "published_at": None,
                "exams_count": len(term_exams),
                "graded_count": 0,
                "total_marks_obtained": None,
                "total_max_marks": None,
                "average_percentage": None,
                "letter_grade": None,
                "pass_fail_status": "UNPUBLISHED",
                "class_rank": None,
                "cohort_size": None,
            }

        term_results = [result_by_exam[e.pk] for e in term_exams if e.pk in result_by_exam]
        total_obtained = sum((r.marks_obtained for r in term_results), Decimal("0.00"))
        total_max = sum((r.exam.max_marks for r in term_results), Decimal("0.00"))
        if term_results and total_max > 0:
            avg_pct = rounded((total_obtained / total_max) * Decimal("100"))
            letter = grade_for(avg_pct)
            all_passed = all(r.passed for r in term_results)
            pf_status = "PASS" if (all_passed and avg_pct >= Decimal("50.00")) else "FAIL"
        else:
            avg_pct = None
            letter = None
            pf_status = "PENDING"

        class_rank = None
        cohort_size = None
        primary_cid = next(iter(primary_class_ids), None)
        if primary_cid:
            cls_obj = class_map.get(primary_cid) or Class.objects.filter(pk=primary_cid).first()
            if cls_obj:
                broadsheet = build_class_broadsheet(term_obj, cls_obj)
                cohort_size = broadsheet["summary"]["enrolled_count"]
                student_row = next(
                    (row for row in broadsheet["students"] if row["student_id"] == selected_student.pk),
                    None,
                )
                if student_row:
                    class_rank = student_row["rank"]

        return {
            "term_id": term_obj.pk,
            "name": term_obj.name,
            "term_type": term_obj.term_type,
            "is_published": term_obj.is_published,
            "published_at": term_obj.published_at.isoformat() if term_obj.published_at else None,
            "exams_count": len(term_exams),
            "graded_count": len(term_results),
            "total_marks_obtained": str(rounded(total_obtained)) if term_results else None,
            "total_max_marks": str(rounded(total_max)) if term_results else None,
            "average_percentage": str(avg_pct) if avg_pct is not None else None,
            "letter_grade": letter,
            "pass_fail_status": pf_status,
            "class_rank": class_rank,
            "cohort_size": cohort_size,
        }

    return {
        "is_admin_preview": staff_viewer,
        "accessible_students": [_serialize_student_ref(s) for s in accessible_students],
        "student": _serialize_student_ref(selected_student),
        "academic_years": years_payload,
        "selected_academic_year": next((yp for yp in years_payload if yp["id"] == selected_year.pk), None),
        "enrolled_subjects": enrolled_subjects,
        "exam_schedule": exam_schedule,
        "results_comparison": results_comparison,
        "term_summaries": {
            "midterm": _build_term_summary(midterm_term),
            "final_term": _build_term_summary(final_term),
        },
    }
