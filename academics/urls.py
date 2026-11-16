from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AcademicYearViewSet,
    BroadsheetAPIView,
    ExamResultViewSet,
    ExamViewSet,
    ParentPortalAPIView,
    TermViewSet,
)

router = DefaultRouter()
router.register("academic-years", AcademicYearViewSet, basename="academic-year")
router.register("terms", TermViewSet, basename="academic-term")
router.register("exams", ExamViewSet, basename="exam")
router.register("exam-results", ExamResultViewSet, basename="exam-result")

urlpatterns = [
    path("academics/broadsheet/", BroadsheetAPIView.as_view(), name="academics-broadsheet"),
    path("academics/parent-portal/", ParentPortalAPIView.as_view(), name="academics-parent-portal"),
] + router.urls
