from django.urls import path

from . import views

app_name = "accounting"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("vendors/", views.vendor_list, name="vendor_list"),
    path("vendors/new/", views.vendor_create, name="vendor_create"),
    path("vendors/<int:vendor_id>/edit/", views.vendor_edit, name="vendor_edit"),
    path("bills/", views.bill_list, name="bill_list"),
    path("bills/new/", views.bill_create, name="bill_create"),
    path("bills/<int:bill_id>/edit/", views.bill_edit, name="bill_edit"),
    path("bills/upload/", views.bill_intake_upload, name="bill_intake_upload"),
    path("bill-intakes/<int:intake_id>/", views.bill_intake_review, name="bill_intake_review"),
    path(
        "bill-intakes/<int:intake_id>/vendor-draft/",
        views.bill_intake_vendor_draft,
        name="bill_intake_vendor_draft",
    ),
    path("bill-intakes/<int:intake_id>/original/", views.bill_intake_original_download, name="bill_intake_original_download"),
    path("bills/<int:bill_id>/", views.bill_detail, name="bill_detail"),
    path("bills/<int:bill_id>/submit/", views.bill_submit, name="bill_submit"),
    path("bills/<int:bill_id>/approve/", views.bill_approve, name="bill_approve"),
    path("bills/<int:bill_id>/reject/", views.bill_reject, name="bill_reject"),
    path("bills/<int:bill_id>/attachments/", views.bill_attachment_upload, name="bill_attachment_upload"),
    path("attachments/<int:attachment_id>/download/", views.attachment_download, name="attachment_download"),
    path("payments/", views.payment_list, name="payment_list"),
    path("payments/new/", views.payment_create, name="payment_create"),
    path("payments/<int:payment_id>/void/", views.payment_void, name="payment_void"),
    path("payments/<int:payment_id>/edit/", views.payment_edit, name="payment_edit"),
    path("aging/", views.aging_report, name="aging_report"),
]
