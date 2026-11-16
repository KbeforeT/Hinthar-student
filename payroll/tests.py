from datetime import date, datetime, time
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from class_sessions.models import AdHocSession, Session
from people.models import Subject, Teacher, User
from timetable.models import Class, TimetableSlot

from .models import CohortRateOverride, TeacherPayout, TeacherRateCard


class PayrollTeacherRateEngineTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="pay-admin", clerk_id="clerk-pay-admin", role="admin"
        )
        self.staff = User.objects.create_user(
            username="pay-staff", clerk_id="clerk-pay-staff", role="staff"
        )
        self.teacher_user = User.objects.create_user(
            username="pay-teacher", clerk_id="clerk-pay-teacher", role="teacher"
        )
        self.client.force_authenticate(self.staff)

        self.teacher1 = Teacher.objects.create(
            name="Dr. Ratio", employment_type="full_time", school_code="HIS"
        )
        self.teacher2 = Teacher.objects.create(
            name="Yae Miko", employment_type="tutor", school_code="HIS"
        )
        self.cohort_k2b = Class.objects.create(
            education_level="IG", cohort_identifier="K", cohort_sub_category="2B"
        )
        self.cohort_k4 = Class.objects.create(
            education_level="IG", cohort_identifier="K", cohort_sub_category="4"
        )
        self.math = Subject.objects.create(name="Mathematics")

        self.slot_k2b = TimetableSlot.objects.create(
            class_obj=self.cohort_k2b,
            subject=self.math,
            teacher=self.teacher1,
            day_of_week=0,
            start_time=time(9, 0),
            end_time=time(10, 30),
            room="R101",
        )
        self.slot_k4 = TimetableSlot.objects.create(
            class_obj=self.cohort_k4,
            subject=self.math,
            teacher=self.teacher1,
            day_of_week=2,
            start_time=time(13, 0),
            end_time=time(15, 0),
            room="R102",
        )

        tz = timezone.get_current_timezone()
        # Completed session in IG K2B (1.5h) taught by teacher1
        Session.objects.create(
            timetable_slot=self.slot_k2b,
            teacher=self.teacher1,
            class_obj=self.cohort_k2b,
            start_time=timezone.make_aware(datetime(2026, 11, 2, 9, 0), tz),
            end_time=timezone.make_aware(datetime(2026, 11, 2, 10, 30), tz),
            status="completed",
        )
        # Scheduled session in IG K4 (2.0h) taught by teacher1
        Session.objects.create(
            timetable_slot=self.slot_k4,
            teacher=self.teacher1,
            class_obj=self.cohort_k4,
            start_time=timezone.make_aware(datetime(2026, 11, 4, 13, 0), tz),
            end_time=timezone.make_aware(datetime(2026, 11, 4, 15, 0), tz),
            status="scheduled",
        )
        # Cancelled session in IG K2B (must NOT be paid)
        Session.objects.create(
            timetable_slot=self.slot_k2b,
            teacher=self.teacher1,
            class_obj=self.cohort_k2b,
            start_time=timezone.make_aware(datetime(2026, 11, 9, 9, 0), tz),
            end_time=timezone.make_aware(datetime(2026, 11, 9, 10, 30), tz),
            status="cancelled",
        )
        # Substitute cover session: assigned to teacher1, actually taught by teacher2!
        Session.objects.create(
            timetable_slot=self.slot_k2b,
            teacher=self.teacher1,
            actual_teacher=self.teacher2,
            class_obj=self.cohort_k2b,
            start_time=timezone.make_aware(datetime(2026, 11, 16, 9, 0), tz),
            end_time=timezone.make_aware(datetime(2026, 11, 16, 10, 30), tz),
            status="completed",
        )
        # Ad-hoc tutoring session taught by teacher2 (1.0h)
        AdHocSession.objects.create(
            teacher=self.teacher2,
            subject=self.math,
            date=date(2026, 11, 18),
            start_time=time(16, 0),
            end_time=time(17, 0),
            status="completed",
        )

    def test_upsert_rate_card_with_cohort_overrides_and_per_session_payout(self):
        # Teacher 1: per-session base = 20,000; IG K4 override = 30,000
        res = self.client.post(
            "/api/v1/teacher-rate-cards/upsert/",
            {
                "teacher_id": self.teacher1.pk,
                "rate_type": "rate_per_session",
                "rate_per_session": "20000.00",
                "hourly_rate": "15000.00",
                "currency": "MMK",
                "cohort_overrides": [
                    {"class_obj_id": self.cohort_k4.pk, "rate_per_session": "30000.00"},
                ],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(len(res.data["cohort_overrides"]), 1)

        # Calculate payouts for 2026-11
        calc = self.client.post(
            "/api/v1/teacher-payouts/calculate/",
            {"month": "2026-11"},
            format="json",
        )
        self.assertEqual(calc.status_code, status.HTTP_200_OK, calc.data)
        payouts = {p["teacher"]: p for p in calc.data["payouts"]}

        p1 = payouts[self.teacher1.pk]
        # Teacher 1 taught 2 sessions: 1 in IG K2B (20,000) + 1 in IG K4 (30,000 override) = 50,000
        # Cancelled session and covered session (actual_teacher=teacher2) are excluded.
        self.assertEqual(p1["sessions_count"], 2)
        self.assertEqual(p1["completed_sessions_count"], 1)
        self.assertEqual(p1["scheduled_sessions_count"], 1)
        self.assertEqual(p1["session_earnings"], "50000.00")
        self.assertEqual(p1["net_payout"], "50000.00")
        self.assertEqual(len(p1["session_breakdown"]["sessions"]), 2)
        self.assertEqual(len(p1["session_breakdown"]["cohorts"]), 2)

    def test_hourly_rate_override_adjustments_and_status_transitions(self):
        # Teacher 2: hourly_rate base = 10,000/hr, IG K2B override = 16,000/hr
        self.client.post(
            "/api/v1/teacher-rate-cards/upsert/",
            {
                "teacher_id": self.teacher2.pk,
                "rate_type": "hourly_rate",
                "rate_per_session": "0.00",
                "hourly_rate": "10000.00",
                "currency": "MMK",
                "cohort_overrides": [
                    {"class_obj_id": self.cohort_k2b.pk, "hourly_rate": "16000.00"},
                ],
            },
            format="json",
        )

        res = self.client.get("/api/v1/teacher-payouts/", {"month": "2026-11"})
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        rows = res.data["results"] if isinstance(res.data, dict) else res.data
        p2 = next(r for r in rows if r["teacher"] == self.teacher2.pk)

        # 1.5h substitute in IG K2B @ 16,000/hr = 24,000 + 1.0h ad-hoc @ 10,000/hr = 10,000 -> 34,000
        self.assertEqual(p2["sessions_count"], 2)
        self.assertEqual(p2["total_hours"], "2.50")
        self.assertEqual(p2["session_earnings"], "34000.00")
        self.assertEqual(p2["status"], "draft")

        # Apply manual bonus (+5,000) and deduction (-2,000) -> 37,000
        patched = self.client.patch(
            f"/api/v1/teacher-payouts/{p2['id']}/",
            {
                "bonus": "5000.00",
                "deduction": "2000.00",
                "adjustment_note": "Substitute cover bonus minus late penalty",
            },
            format="json",
        )
        self.assertEqual(patched.status_code, status.HTTP_200_OK, patched.data)
        self.assertEqual(patched.data["net_payout"], "37000.00")

        # Transition: draft -> approved -> paid
        approved = self.client.post(
            f"/api/v1/teacher-payouts/{p2['id']}/transition/",
            {"status": "approved"},
            format="json",
        )
        self.assertEqual(approved.status_code, status.HTTP_200_OK, approved.data)
        self.assertEqual(approved.data["status"], "approved")
        self.assertIsNotNone(approved.data["approved_at"])
        self.assertIsNone(approved.data["paid_at"])

        paid = self.client.post(
            f"/api/v1/teacher-payouts/{p2['id']}/transition/",
            {"status": "paid"},
            format="json",
        )
        self.assertEqual(paid.status_code, status.HTTP_200_OK, paid.data)
        self.assertEqual(paid.data["status"], "paid")
        self.assertIsNotNone(paid.data["paid_at"])

    def test_non_staff_cannot_access_payroll(self):
        self.client.force_authenticate(self.teacher_user)
        self.assertEqual(self.client.get("/api/v1/teacher-rate-cards/").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get("/api/v1/teacher-payouts/").status_code, status.HTTP_403_FORBIDDEN)
