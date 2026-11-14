from django.urls import path
from catalog import views

urlpatterns = [
    path("", views.root),
    path("login", views.login_page, name="login"),
    path("logout", views.logout_view),
    path("jobs/", views.job_list),
    path("slices/", views.slice_board),
    path("jobs/<int:job_id>/", views.job_detail),
    path("jobs/<int:job_id>/policy/", views.job_policy),
    path("jobs/<int:job_id>/claim/", views.slice_claim),
    path("jobs/<int:job_id>/slices/<int:slice_no>/result/", views.slice_result),
    path("jobs/<int:job_id>/recycle/", views.job_recycle),
    path("jobs/<int:job_id>/assemble/", views.job_assemble),
]
