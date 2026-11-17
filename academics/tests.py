from datetime import date, time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from people.models import Student, Subject, User
from timetable.models import Class, ClassStudent, Course, StudentSubjectEnrollment

from .models import (
    AcademicYear,
    Exam,
    ExamResult,
    ExamSchedule,
    Term,
    format_student_identifier,
    grade_for,
)


class AcademicsExamTrackingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="acad-admin", clerk_id="clerk-acad-admin", role="admin"
        )
        cls.staff = User.objects.create_user(
            username="acad-staff", clerk_id="clerk-acad-staff", role="staff"
        )
        cls.parent_user = User.objects.create_user(
            username="acad-parent", clerk_id="clerk-acad-parent", role="parent"
        )
        cls.other_parent = User.objects.create_user(
            username="acad-other-parent", clerk_id="clerk-acad-other-parent", role="parent"
        )
        cls.student_user = User.objects.create_user(
            username="acad-student", clerk_id="clerk-acad-student", role="student"
        )
        cls.teacher_user = User.objects.create_user(
            username="acad-teacher", clerk_id="clerk-acad-teacher", role="teacher"
        )

        cls.cohort = Class.objects.create(
            education_level="IG", cohort_identifier="K", cohort_sub_category="2B"
        )
        cls.other_cohort = Class.objects.create(
            education_level="IG", cohort_identifier="K", cohort_sub_category="4"
        )

        cls.student1 = Student.objects.create(
            name="Aung Myint",
            school_code="SPD",
            user=cls.student_user,
            parent=cls.parent_user,
        )
        cls.student2 = Student.objects.create(
            name="Su Su Khin",
            school_code="SPD",
            parent=cls.parent_user,
        )
        cls.student3 = Student.objects.create(
            name="Min Khant",
            school_code="SPD",
        )
        cls.outside_student = Student.objects.create(
            name="Other Cohort Kid",
            school_code="SPD",
            parent=cls.other_parent,
        )

        ClassStudent.objects.create(student=cls.student1, class_obj=cls.cohort)
        ClassStudent.objects.create(student=cls.student2, class_obj=cls.cohort)
        ClassStudent.objects.create(student=cls.student3, class_obj=cls.cohort)
        ClassStudent.objects.create(student=cls.outside_student, class_obj=cls.other_cohort)

        cls.math = Subject.objects.create(name="Mathematics")
        cls.physics = Subject.objects.create(name="Physics")
        cls.chemistry = Subject.objects.create(name="Chemistry")

        Course.objects.create(class_obj=cls.cohort, subject=cls.math)
        Course.objects.create(class_obj=cls.cohort, subject=cls.physics)
        Course.objects.create(class_obj=cls.other_cohort, subject=cls.chemistry)

        # Student 1 also cross-enrolls into Chemistry in IG K4
        StudentSubjectEnrollment.objects.create(
            student=cls.student1,
            class_obj=cls.other_cohort,
            subject=cls.chemistry,
            status="enrolled",
        )

        cls.year = AcademicYear.objects.create(
            name="2026-2027",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
        )
        cls.midterm = cls.year.terms.get(number=1)
        cls.final_term = cls.year.terms.get(number=2)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.staff)

    def test_academic_year_auto_generates_fixed_midterm_and_final_terms(self):
        res = self.client.post(
            "/api/v1/academic-years/",
            {
                "name": "2027-2028",
                "start_date": "2027-06-01",
                "end_date": "2028-05-31",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        terms = res.data["terms"]
        self.assertEqual(len(terms), 2)
        self.assertEqual([t["name"] for t in terms], ["Midterm", "Final Term"])
        self.assertEqual([t["term_type"] for t in terms], ["midterm", "final"])
        self.assertEqual([t["is_published"] for t in terms], [False, False])

        bad = self.client.post(
            "/api/v1/academic-years/",
            {"name": "bad-year", "start_date": "2027-06-01", "end_date": "2027-01-01"},
            format="json",
        )
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)

        with self.assertRaises(ValidationError):
            Term.objects.create(academic_year=self.year, number=3)

    def test_term_publish_toggle(self):
        self.assertFalse(self.midterm.is_published)
        self.assertIsNone(self.midterm.published_at)

        res = self.client.post(f"/api/v1/terms/{self.midterm.pk}/toggle_publish/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertTrue(res.data["is_published"])
        self.assertIsNotNone(res.data["published_at"])

        res2 = self.client.post(f"/api/v1/terms/{self.midterm.pk}/unpublish/", {}, format="json")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertFalse(res2.data["is_published"])
        self.assertIsNone(res2.data["published_at"])

    def test_exam_scheduling_defaults_and_cohort_overlap_conflict(self):
        # Create exam with defaults max_marks=100, pass_marks=50
        res = self.client.post(
            "/api/v1/exams/",
            {
                "term": self.midterm.pk,
                "class_obj": self.cohort.pk,
                "subject": self.math.pk,
                "exam_date": "2026-10-12",
                "start_time": "09:00",
                "end_time": "11:00",
                "room": "HALL-A",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(Decimal(res.data["max_marks"]), Decimal("100.00"))
        self.assertEqual(Decimal(res.data["pass_marks"]), Decimal("50.00"))

        # Conflict pre-check endpoint detects overlap for same cohort
        conflict_res = self.client.get(
            "/api/v1/exams/conflicts/",
            {
                "class_id": self.cohort.pk,
                "exam_date": "2026-10-12",
                "start_time": "10:30",
                "end_time": "12:00",
                "room": "HALL-B",
            },
        )
        self.assertEqual(conflict_res.status_code, status.HTTP_200_OK)
        self.assertTrue(conflict_res.data["has_conflicts"])
        self.assertEqual(len(conflict_res.data["cohort_conflicts"]), 1)

        # Creating overlapping exam for same cohort fails with 400
        overlap = self.client.post(
            "/api/v1/exams/",
            {
                "term": self.midterm.pk,
                "class_obj": self.cohort.pk,
                "subject": self.physics.pk,
                "exam_date": "2026-10-12",
                "start_time": "10:00",
                "end_time": "12:00",
                "room": "HALL-B",
            },
            format="json",
        )
        self.assertEqual(overlap.status_code, status.HTTP_400_BAD_REQUEST)

        # Non-overlapping slot on same day succeeds
        ok = self.client.post(
            "/api/v1/exams/",
            {
                "term": self.midterm.pk,
                "class_obj": self.cohort.pk,
                "subject": self.physics.pk,
                "exam_date": "2026-10-12",
                "start_time": "13:00",
                "end_time": "15:00",
                "room": "HALL-A",
            },
            format="json",
        )
        self.assertEqual(ok.status_code, status.HTTP_201_CREATED, ok.data)

    def test_bulk_mark_entry_and_class_broadsheet_with_ranks(self):
        math_exam = Exam.objects.create(
            term=self.midterm,
            class_obj=self.cohort,
            subject=self.math,
            exam_date=date(2026, 10, 12),
            start_time=time(9, 0),
            end_time=time(11, 0),
            room="HALL-A",
            max_marks=Decimal("100.00"),
            pass_marks=Decimal("50.00"),
        )
        phys_exam = Exam.objects.create(
            term=self.midterm,
            class_obj=self.cohort,
            subject=self.physics,
            exam_date=date(2026, 10, 13),
            start_time=time(9, 0),
            end_time=time(11, 0),
            room="HALL-A",
            max_marks=Decimal("100.00"),
            pass_marks=Decimal("50.00"),
        )

        # Out-of-range or unenrolled student fails
        bad_marks = self.client.post(
            f"/api/v1/exams/{math_exam.pk}/bulk_marks/",
            {
                "records": [
                    {"student_id": self.outside_student.pk, "marks_obtained": "80"},
                ]
            },
            format="json",
        )
        self.assertEqual(bad_marks.status_code, status.HTTP_400_BAD_REQUEST)

        # Enter marks so student1 and student2 tie for rank 1 (90% avg), student3 fails (40% avg)
        res1 = self.client.post(
            f"/api/v1/exams/{math_exam.pk}/bulk_marks/",
            {
                "records": [
                    {"student_id": self.student1.pk, "marks_obtained": "90", "remarks": "Top work"},
                    {"student_id": self.student2.pk, "marks_obtained": "85", "remarks": "Great"},
                    {"student_id": self.student3.pk, "marks_obtained": "40", "remarks": "Needs help"},
                ]
            },
            format="json",
        )
        self.assertEqual(res1.status_code, status.HTTP_200_OK, res1.data)
        self.assertEqual(res1.data["saved_count"], 3)
        self.assertIn("broadsheet", res1.data)
        self.assertEqual(res1.data["broadsheet"]["summary"]["graded_students_count"], 3)

        res2 = self.client.post(
            "/api/v1/exam-results/bulk_upsert/",
            {
                "records": [
                    {"exam_id": phys_exam.pk, "student_id": self.student1.pk, "marks_obtained": "90"},
                    {"exam_id": phys_exam.pk, "student_id": self.student2.pk, "marks_obtained": "95"},
                    {"exam_id": phys_exam.pk, "student_id": self.student3.pk, "marks_obtained": "40"},
                ]
            },
            format="json",
        )
        self.assertEqual(res2.status_code, status.HTTP_200_OK, res2.data)
        self.assertIn("broadsheet", res2.data)

        broadsheet = self.client.get(
            "/api/v1/academics/broadsheet/",
            {"term_id": self.midterm.pk, "class_id": self.cohort.pk},
        )
        self.assertEqual(broadsheet.status_code, status.HTTP_200_OK, broadsheet.data)
        data = broadsheet.data
        self.assertEqual(data["summary"]["enrolled_count"], 3)
        self.assertEqual(data["summary"]["pass_count"], 2)
        self.assertEqual(data["summary"]["fail_count"], 1)
        self.assertEqual(data["summary"]["pass_rate"], "66.67")

        by_id = {row["student_id"]: row for row in data["students"]}
        self.assertEqual(by_id[self.student1.pk]["rank"], 1)
        self.assertEqual(by_id[self.student2.pk]["rank"], 1)
        self.assertEqual(by_id[self.student3.pk]["rank"], 3)
        self.assertEqual(by_id[self.student1.pk]["letter_grade"], "A*")
        self.assertEqual(by_id[self.student1.pk]["pass_fail_status"], "PASS")
        self.assertEqual(by_id[self.student3.pk]["letter_grade"], "F")
        self.assertEqual(by_id[self.student3.pk]["pass_fail_status"], "FAIL")
        self.assertTrue(by_id[self.student1.pk]["unique_code"].startswith("SPD"))
        self.assertEqual(by_id[self.student1.pk]["student_code"], by_id[self.student1.pk]["unique_code"])

    def test_parent_portal_scoping_publish_gate_and_comparison(self):
        mid_math = Exam.objects.create(
            term=self.midterm,
            class_obj=self.cohort,
            subject=self.math,
            exam_date=date(2026, 10, 12),
            start_time=time(9, 0),
            end_time=time(11, 0),
            room="R201",
        )
        fin_math = Exam.objects.create(
            term=self.final_term,
            class_obj=self.cohort,
            subject=self.math,
            exam_date=date(2027, 3, 15),
            start_time=time(9, 0),
            end_time=time(11, 0),
            room="R201",
        )
        mid_chem = Exam.objects.create(
            term=self.midterm,
            class_obj=self.other_cohort,
            subject=self.chemistry,
            exam_date=date(2026, 10, 14),
            start_time=time(9, 0),
            end_time=time(11, 0),
            room="LAB-1",
        )

        ExamResult.objects.create(
            exam=mid_math, student=self.student1, marks_obtained=Decimal("72"), remarks="Good midterm"
        )
        ExamResult.objects.create(
            exam=fin_math, student=self.student1, marks_obtained=Decimal("88"), remarks="Strong final"
        )
        ExamResult.objects.create(
            exam=mid_chem, student=self.student1, marks_obtained=Decimal("80"), remarks="Lab work solid"
        )

        # Authenticate as parent_user while both terms are UNPUBLISHED
        self.client.force_authenticate(self.parent_user)
        res = self.client.get("/api/v1/academics/parent-portal/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertFalse(res.data["is_admin_preview"])
        self.assertEqual({s["id"] for s in res.data["accessible_students"]}, {self.student1.pk, self.student2.pk})
        self.assertEqual(len(res.data["exam_schedule"]), 3)

        # Unpublished marks are masked for parents
        math_row = next(r for r in res.data["results_comparison"] if r["subject_id"] == self.math.pk)
        self.assertEqual(math_row["midterm"]["status"], "UNPUBLISHED")
        self.assertIsNone(math_row["midterm"]["marks_obtained"])
        self.assertEqual(math_row["final_term"]["status"], "UNPUBLISHED")
        self.assertIsNone(math_row["delta_percentage"])

        # Parent cannot view another family's student
        denied = self.client.get(
            "/api/v1/academics/parent-portal/", {"student_id": self.outside_student.pk}
        )
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

        # Admin publishes Midterm and Final Term
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/v1/terms/{self.midterm.pk}/publish/")
        self.client.post(f"/api/v1/terms/{self.final_term.pk}/publish/")

        # Parent now sees full Midterm vs Final Term comparison & +16.00 delta
        self.client.force_authenticate(self.parent_user)
        res_pub = self.client.get(
            "/api/v1/academics/parent-portal/", {"student_id": self.student1.pk}
        )
        self.assertEqual(res_pub.status_code, status.HTTP_200_OK, res_pub.data)
        math_pub = next(r for r in res_pub.data["results_comparison"] if r["subject_id"] == self.math.pk)
        self.assertEqual(math_pub["midterm"]["marks_obtained"], "72.00")
        self.assertEqual(math_pub["midterm"]["grade"], "B")
        self.assertEqual(math_pub["final_term"]["marks_obtained"], "88.00")
        self.assertEqual(math_pub["final_term"]["grade"], "A")
        self.assertEqual(math_pub["delta_percentage"], "16.00")

        # Admin preview works for any student
        self.client.force_authenticate(self.admin)
        preview = self.client.get(
            "/api/v1/academics/parent-portal/", {"student_id": self.student1.pk}
        )
        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertTrue(preview.data["is_admin_preview"])

    def test_grade_bands(self):
        for pct, expected in [
            ("0", "F"),
            ("49.99", "F"),
            ("50", "D"),
            ("59.99", "D"),
            ("60", "C"),
            ("69.99", "C"),
            ("70", "B"),
            ("79.99", "B"),
            ("80", "A"),
            ("89.99", "A"),
            ("90", "A*"),
            ("100", "A*"),
        ]:
            self.assertEqual(grade_for(Decimal(pct)), expected)

    def test_student_identifier_and_readable_audit_strings(self):
        from types import SimpleNamespace
        from people.audit import clean_audit_summary

        self.assertEqual(
            format_student_identifier(SimpleNamespace(unique_code="DUMMY-IGK3-004", pk=4, enrollment_date=date(2026, 6, 1), school_code="SPD")),
            "SPD26-00004",
        )
        self.assertEqual(
            format_student_identifier(SimpleNamespace(unique_code="#0001-2023-001", pk=1, enrollment_date=date(2026, 6, 1), school_code="SPD")),
            "SPD26-00001",
        )
        self.assertEqual(
            format_student_identifier(SimpleNamespace(unique_code="SPD26-00043", pk=43, enrollment_date=date(2026, 6, 1), school_code="SPD")),
            "SPD26-00043",
        )

        self.assertIs(ExamSchedule, Exam)
        self.assertEqual(
            clean_audit_summary("Created report card ReportCard object (1)"),
            "Created report card #1",
        )
        self.assertEqual(
            clean_audit_summary("Created assessment result AssessmentResult object (3)"),
            "Created assessment result #3",
        )

    def test_academic_year_conflict_and_label_validation(self):
        # Reversed label like 2028-2027 must be rejected
        reversed_year = self.client.post(
            "/api/v1/academic-years/",
            {"name": "2028-2027", "start_date": "2026-06-01", "end_date": "2027-05-31"},
            format="json",
        )
        self.assertEqual(reversed_year.status_code, status.HTTP_400_BAD_REQUEST)

        # Non-consecutive label like 2027-2029 must be rejected
        non_consecutive = self.client.post(
            "/api/v1/academic-years/",
            {"name": "2027-2029", "start_date": "2027-06-01", "end_date": "2029-05-31"},
            format="json",
        )
        self.assertEqual(non_consecutive.status_code, status.HTTP_400_BAD_REQUEST)

        # Mismatched date years for label 2027-2028 must be rejected
        mismatched = self.client.post(
            "/api/v1/academic-years/",
            {"name": "2027-2028", "start_date": "2026-06-01", "end_date": "2027-05-31"},
            format="json",
        )
        self.assertEqual(mismatched.status_code, status.HTTP_400_BAD_REQUEST)

        # Overlapping date window with existing 2026-2027 (2026-06-01 .. 2027-05-31) must be rejected
        overlap = self.client.post(
            "/api/v1/academic-years/",
            {"name": "2027-2028", "start_date": "2027-05-15", "end_date": "2028-05-31"},
            format="json",
        )
        self.assertEqual(overlap.status_code, status.HTTP_400_BAD_REQUEST)

    def test_exam_date_outside_academic_year_rejected_and_flagged(self):
        outside = self.client.post(
            "/api/v1/exams/",
            {
                "term": self.midterm.pk,
                "class_obj": self.cohort.pk,
                "subject": self.math.pk,
                "exam_date": "2028-10-12",
                "start_time": "09:00",
                "end_time": "11:00",
                "room": "HALL-A",
            },
            format="json",
        )
        self.assertEqual(outside.status_code, status.HTTP_400_BAD_REQUEST)

        precheck = self.client.get(
            "/api/v1/exams/conflicts/",
            {
                "class_id": self.cohort.pk,
                "term_id": self.midterm.pk,
                "subject_id": self.math.pk,
                "exam_date": "2028-10-12",
                "start_time": "09:00",
                "end_time": "11:00",
            },
        )
        self.assertEqual(precheck.status_code, status.HTTP_200_OK)
        self.assertTrue(precheck.data["has_conflicts"])
        self.assertTrue(
            any("outside Academic Year" in c["message"] for c in precheck.data["cohort_conflicts"])
        )

    def test_denied_roles_on_admin_endpoints(self):
        self.client.force_authenticate(self.teacher_user)
        self.assertEqual(self.client.get("/api/v1/exams/").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get("/api/v1/academics/parent-portal/").status_code, status.HTTP_403_FORBIDDEN)
