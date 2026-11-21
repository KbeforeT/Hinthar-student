from rest_framework.routers import DefaultRouter

from .views import (
    CohortRateOverrideViewSet,
    TeacherPayoutViewSet,
    TeacherRateCardViewSet,
)

router = DefaultRouter()
router.register("teacher-rate-cards", TeacherRateCardViewSet, basename="teacher-rate-card")
router.register("cohort-rate-overrides", CohortRateOverrideViewSet, basename="cohort-rate-override")
router.register("teacher-payouts", TeacherPayoutViewSet, basename="teacher-payout")

urlpatterns = router.urls
