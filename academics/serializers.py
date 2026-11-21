from rest_framework import serializers

from .models import AcademicYear, Exam, ExamResult, Term, format_student_identifier


class TermSerializer(serializers.ModelSerializer):
    name = serializers.CharField(read_only=True)
    academic_year_name = serializers.CharField(source="academic_year.name", read_only=True)

    class Meta:
        model = Term
        fields = [
            "id",
            "academic_year",
            "academic_year_name",
            "number",
            "term_type",
            "name",
            "is_published",
            "published_at",
            "published_by",
        ]
        read_only_fields = [
            "id",
            "academic_year",
            "academic_year_name",
            "number",
            "term_type",
            "name",
            "published_at",
            "published_by",
        ]


class AcademicYearSerializer(serializers.ModelSerializer):
    terms = TermSerializer(many=True, read_only=True)

    class Meta:
        model = AcademicYear
        fields = ["id", "name", "start_date", "end_date", "is_active", "terms"]
        read_only_fields = ["id", "terms"]


class ExamSerializer(serializers.ModelSerializer):
    class_label = serializers.CharField(source="class_obj.__str__", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    term_name = serializers.CharField(source="term.name", read_only=True)
    term_type = serializers.CharField(source="term.term_type", read_only=True)
    academic_year_id = serializers.IntegerField(source="term.academic_year_id", read_only=True)
    academic_year_name = serializers.CharField(source="term.academic_year.name", read_only=True)
    results_count = serializers.SerializerMethodField()

    class Meta:
        model = Exam
        fields = [
            "id",
            "term",
            "term_name",
            "term_type",
            "academic_year_id",
            "academic_year_name",
            "class_obj",
            "class_label",
            "subject",
            "subject_name",
            "title",
            "exam_date",
            "start_time",
            "end_time",
            "room",
            "max_marks",
            "pass_marks",
            "results_count",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "term_name",
            "term_type",
            "academic_year_id",
            "academic_year_name",
            "class_label",
            "subject_name",
            "results_count",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def get_results_count(self, obj):
        if hasattr(obj, "_results_count"):
            return obj._results_count
        return obj.results.count()


class ExamResultSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.name", read_only=True)
    student_code = serializers.SerializerMethodField()
    exam_title = serializers.CharField(source="exam.title", read_only=True)
    max_marks = serializers.DecimalField(
        source="exam.max_marks", max_digits=8, decimal_places=2, read_only=True
    )
    pass_marks = serializers.DecimalField(
        source="exam.pass_marks", max_digits=8, decimal_places=2, read_only=True
    )
    percentage = serializers.DecimalField(max_digits=6, decimal_places=2, read_only=True)
    grade = serializers.CharField(read_only=True)
    passed = serializers.BooleanField(read_only=True)
    status = serializers.CharField(read_only=True)

    class Meta:
        model = ExamResult
        fields = [
            "id",
            "exam",
            "exam_title",
            "student",
            "student_name",
            "student_code",
            "marks_obtained",
            "max_marks",
            "pass_marks",
            "is_absent",
            "percentage",
            "grade",
            "passed",
            "status",
            "remarks",
            "recorded_by",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "exam_title",
            "student_name",
            "student_code",
            "max_marks",
            "pass_marks",
            "percentage",
            "grade",
            "passed",
            "status",
            "recorded_by",
            "updated_at",
        ]

    def get_student_code(self, obj):
        if obj.student_id and obj.student:
            return format_student_identifier(obj.student)
        return None
