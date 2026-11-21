from django.contrib import admin

from .models import CohortRateOverride, TeacherPayout, TeacherRateCard


@admin.register(TeacherRateCard)
class TeacherRateCardAdmin(admin.ModelAdmin):
    list_display = ("id", "teacher", "rate_type", "rate_per_session", "hourly_rate", "currency", "updated_at")


@admin.register(CohortRateOverride)
class CohortRateOverrideAdmin(admin.ModelAdmin):
    list_display = ("id", "rate_card", "class_obj", "rate_per_session", "hourly_rate")


@admin.register(TeacherPayout)
class TeacherPayoutAdmin(admin.ModelAdmin):
    list_display = ("id", "teacher", "month", "sessions_count", "session_earnings", "bonus", "deduction", "net_payout", "status")
    list_filter = ("status", "month")
