from django.contrib import admin

from .models import AcademicYear, Exam, ExamResult, Term


@admin.register(AcademicYear)
class AcademicYearAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "start_date", "end_date", "is_active")


@admin.register(Term)
class TermAdmin(admin.ModelAdmin):
    list_display = ("id", "academic_year", "number", "term_type", "is_published", "published_at")
    list_filter = ("term_type", "is_published", "academic_year")


@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ("id", "term", "class_obj", "subject", "exam_date", "start_time", "end_time", "room")
    list_filter = ("term", "class_obj", "subject")


@admin.register(ExamResult)
class ExamResultAdmin(admin.ModelAdmin):
    list_display = ("id", "exam", "student", "marks_obtained", "is_absent", "updated_at")
    list_filter = ("is_absent", "exam__term")
