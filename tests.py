from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from .models import (
    AccountingAuditLog,
    Bill,
    BillAttachment,
    BillIntake,
    BillLine,
    Payment,
    Vendor,
)
from .services import record_payment_for_bill
from .intake import OCRUnavailable


User = get_user_model()


class AccountsPayableTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(
            username="ap-creator", password="test-password", is_staff=True
        )
        self.approver = User.objects.create_user(
            username="ap-approver", password="test-password", is_staff=True
        )
        self.portal_user = User.objects.create_user(
            username="portal-user", password="test-password"
        )
        self.vendor = Vendor.objects.create(legal_name="Example Vendor")

    def create_bill(self, **overrides):
        bill = Bill.objects.create(
            vendor=self.vendor,
            bill_number=overrides.pop("bill_number", "INV-100"),
            invoice_date=overrides.pop("invoice_date", timezone.localdate()),
            received_date=overrides.pop("received_date", timezone.localdate()),
            due_date=overrides.pop("due_date", timezone.localdate() + timedelta(days=10)),
            created_by=overrides.pop("created_by", self.creator),
            **overrides,
        )
        BillLine.objects.create(
            bill=bill, description="Credentialing work", quantity=Decimal("2"), unit_price=Decimal("50")
        )
        bill.refresh_from_db()
        return bill

    def submit_and_approve(self, bill):
        self.client.force_login(self.creator)
        self.client.post(reverse("accounting:bill_submit", args=[bill.pk]))
        self.client.force_login(self.approver)
        self.client.post(reverse("accounting:bill_approve", args=[bill.pk]))
        bill.refresh_from_db()
        return bill

    def test_vendor_creation_assigns_number_and_audits(self):
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("accounting:vendor_create"),
            {
                "legal_name": "New Vendor LLC",
                "display_name": "New Vendor",
                "active": "on",
                "payment_terms_days": 30,
                "default_payment_method": Vendor.PaymentMethod.ACH,
            },
        )
        self.assertRedirects(response, reverse("accounting:vendor_list"))
        vendor = Vendor.objects.get(legal_name="New Vendor LLC")
        self.assertTrue(vendor.vendor_number.startswith("VND-"))
        self.assertTrue(
            AccountingAuditLog.objects.filter(entity_id=vendor.pk, action="vendor_created").exists()
        )

    def test_bill_amount_is_calculated_from_lines_and_tax(self):
        bill = self.create_bill(tax=Decimal("7.50"))
        self.assertEqual(bill.subtotal, Decimal("100.00"))
        self.assertEqual(bill.total, Decimal("107.50"))

    def test_creator_cannot_approve_but_different_staff_can(self):
        bill = self.create_bill()
        self.client.force_login(self.creator)
        self.client.post(reverse("accounting:bill_submit", args=[bill.pk]))
        self.client.post(reverse("accounting:bill_approve", args=[bill.pk]))
        bill.refresh_from_db()
        self.assertEqual(bill.status, Bill.Status.SUBMITTED)

        self.client.force_login(self.approver)
        self.client.post(reverse("accounting:bill_approve", args=[bill.pk]))
        bill.refresh_from_db()
        self.assertEqual(bill.status, Bill.Status.APPROVED)
        self.assertEqual(bill.approved_by, self.approver)

    def test_payment_cannot_be_recorded_before_approval(self):
        bill = self.create_bill()
        with self.assertRaises(ValidationError):
            record_payment_for_bill(
                bill_id=bill.pk,
                actor=self.creator,
                amount=Decimal("20.00"),
                payment_date=timezone.localdate(),
                method=Vendor.PaymentMethod.ACH,
                status=Payment.Status.PROCESSED,
            )
        self.assertEqual(Payment.objects.count(), 0)

    def test_processed_payment_allocation_marks_bill_paid(self):
        bill = self.submit_and_approve(self.create_bill())
        payment = record_payment_for_bill(
            bill_id=bill.pk,
            actor=self.creator,
            amount=Decimal("100.00"),
            payment_date=timezone.localdate(),
            method=Vendor.PaymentMethod.CHECK,
            status=Payment.Status.PROCESSED,
        )
        bill.refresh_from_db()
        self.assertEqual(payment.allocations.count(), 1)
        self.assertEqual(bill.amount_paid, Decimal("100.00"))
        self.assertEqual(bill.balance_due, Decimal("0.00"))
        self.assertEqual(bill.status, Bill.Status.PAID)

    def test_aging_report_places_overdue_bill_in_correct_band(self):
        bill = self.create_bill(
            bill_number="INV-AGED",
            due_date=timezone.localdate() - timedelta(days=45),
        )
        self.submit_and_approve(bill)
        self.client.force_login(self.creator)
        response = self.client.get(reverse("accounting:aging_report"))
        self.assertContains(response, "31-60")
        self.assertContains(response, "INV-AGED")

    def test_non_staff_cannot_access_accounting(self):
        self.client.force_login(self.portal_user)
        response = self.client.get(reverse("accounting:dashboard"))
        self.assertEqual(response.status_code, 302)

    def test_bill_attachment_download_is_staff_only(self):
        bill = self.create_bill()
        attachment = BillAttachment.objects.create(
            bill=bill,
            file=SimpleUploadedFile("invoice.txt", b"private invoice"),
            original_name="invoice.txt",
            uploaded_by=self.creator,
        )
        self.client.force_login(self.portal_user)
        denied = self.client.get(reverse("accounting:attachment_download", args=[attachment.pk]))
        self.assertEqual(denied.status_code, 302)
        self.client.force_login(self.creator)
        allowed = self.client.get(reverse("accounting:attachment_download", args=[attachment.pk]))
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(b"".join(allowed.streaming_content), b"private invoice")

    def _upload_intake(self, name="invoice.pdf"):
        return self.client.post(
            reverse("accounting:bill_intake_upload"),
            {"original_file": SimpleUploadedFile(name, b"%PDF-1.4", content_type="application/pdf")},
        )

    @patch("accounting.views.extract_document_text")
    def test_upload_prefills_invoice_fields_from_local_text(self, extract_text):
        extract_text.return_value = (
            "Vendor: Example Vendor\nInvoice Number: INV-LOCAL-1\n"
            "Invoice Date: 2026-08-01\nDue Date: 2026-08-31\n"
            "Subtotal: $100.00\nTax: $5.00\nTotal: $105.00"
        )
        self.client.force_login(self.creator)
        response = self._upload_intake()
        intake = BillIntake.objects.get()
        self.assertRedirects(response, reverse("accounting:bill_intake_review", args=[intake.pk]))
        review = self.client.get(response.url)
        self.assertContains(review, "INV-LOCAL-1")
        self.assertContains(review, "Example Vendor")
        self.assertContains(review, "detected total: $105.00")
        self.assertEqual(intake.extraction_status, BillIntake.ExtractionStatus.EXTRACTED)

    @patch("accounting.views.extract_document_text", return_value="Vendor: Unknown Company\nInvoice Number: NEW-1")
    def test_review_submit_creates_draft_and_source_attachment(self, _):
        self.client.force_login(self.creator)
        upload = self._upload_intake()
        intake = BillIntake.objects.get()
        response = self.client.post(
            upload.url,
            {
                "vendor": self.vendor.pk,
                "bill_number": "NEW-1",
                "invoice_date": "2026-08-01",
                "received_date": "2026-08-02",
                "due_date": "2026-08-31",
                "currency": "USD",
                "description": "Imported invoice",
                "tax": "0.00",
                "lines-TOTAL_FORMS": "1",
                "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "1",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-description": "Services",
                "lines-0-quantity": "1",
                "lines-0-unit_price": "100.00",
            },
        )
        bill = Bill.objects.get(bill_number="NEW-1")
        self.assertRedirects(response, reverse("accounting:bill_detail", args=[bill.pk]))
        self.assertEqual(bill.status, Bill.Status.DRAFT)
        self.assertEqual(bill.attachments.count(), 1)
        intake.refresh_from_db()
        self.assertEqual(intake.resulting_bill, bill)
        self.assertTrue(
            AccountingAuditLog.objects.filter(entity_id=bill.pk, action="bill_imported").exists()
        )

    @patch("accounting.views.extract_document_text", return_value="Vendor: Unmatched Vendor\nInvoice Number: NO-VENDOR")
    def test_unknown_vendor_is_not_created_from_extraction(self, _):
        self.client.force_login(self.creator)
        self._upload_intake()
        self.assertEqual(Vendor.objects.count(), 1)
        review = self.client.get(reverse("accounting:bill_intake_review", args=[BillIntake.objects.get().pk]))
        self.assertContains(review, "Choose an existing vendor")

    @patch("accounting.views.extract_document_text", return_value="Vendor: Example Vendor\nInvoice Number: INV-100")
    def test_duplicate_imported_bill_is_rejected(self, _):
        self.create_bill(bill_number="INV-100")
        self.client.force_login(self.creator)
        upload = self._upload_intake()
        response = self.client.post(
            upload.url,
            {
                "vendor": self.vendor.pk,
                "bill_number": "INV-100",
                "invoice_date": "2026-08-01",
                "received_date": "2026-08-02",
                "due_date": "2026-08-31",
                "currency": "USD",
                "tax": "0.00",
                "lines-TOTAL_FORMS": "1",
                "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "1",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-description": "Services",
                "lines-0-quantity": "1",
                "lines-0-unit_price": "100.00",
            },
        )
        self.assertContains(response, "already exists")
        self.assertEqual(Bill.objects.filter(bill_number="INV-100").count(), 1)
        self.assertIsNone(BillIntake.objects.get().resulting_bill)

    @patch("accounting.views.extract_document_text", side_effect=OCRUnavailable("Local Tesseract is not installed."))
    def test_missing_local_ocr_preserves_image_for_review(self, _):
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("accounting:bill_intake_upload"),
            {"original_file": SimpleUploadedFile("scan.png", b"not-a-real-image", content_type="image/png")},
        )
        intake = BillIntake.objects.get()
        self.assertRedirects(response, reverse("accounting:bill_intake_review", args=[intake.pk]))
        self.assertEqual(intake.extraction_status, BillIntake.ExtractionStatus.NEEDS_REVIEW)
        self.assertEqual(intake.extraction_error, "Local Tesseract is not installed.")
        self.client.force_login(self.portal_user)
        denied = self.client.get(reverse("accounting:bill_intake_original_download", args=[intake.pk]))
        self.assertEqual(denied.status_code, 302)

    @patch("accounting.views.extract_document_text", return_value="Vendor: Unmatched Company\nInvoice Number: NEW-2")
    def test_unmatched_intake_vendor_is_an_explicit_editable_draft(self, _):
        self.client.force_login(self.creator)
        self._upload_intake()
        intake = BillIntake.objects.get()
        review = self.client.get(reverse("accounting:bill_intake_review", args=[intake.pk]))
        self.assertContains(review, "Vendor draft from upload")
        self.assertContains(review, "Unmatched Company")
        self.assertEqual(Vendor.objects.count(), 1)

        saved = self.client.post(
            reverse("accounting:bill_intake_vendor_draft", args=[intake.pk]),
            {
                "legal_name": "Unmatched Company",
                "display_name": "Unmatched Company",
                "active": "on",
                "payment_terms_days": 30,
                "default_payment_method": Vendor.PaymentMethod.CHECK,
            },
        )
        self.assertRedirects(saved, reverse("accounting:bill_intake_review", args=[intake.pk]))
        vendor = Vendor.objects.get(legal_name="Unmatched Company")
        intake.refresh_from_db()
        self.assertEqual(intake.reviewed_vendor, vendor)
        self.assertTrue(
            AccountingAuditLog.objects.filter(
                entity_id=vendor.pk, action="vendor_created_from_intake"
            ).exists()
        )

    def test_generated_references_are_editable_and_assigned_when_blank(self):
        self.client.force_login(self.creator)
        vendor_page = self.client.get(reverse("accounting:vendor_create"))
        self.assertContains(vendor_page, "Generated reference; staff may replace it.")
        proposed_vendor_number = vendor_page.context["form"]["vendor_number"].value()
        response = self.client.post(
            reverse("accounting:vendor_create"),
            {
                "legal_name": "Generated Vendor",
                "vendor_number": proposed_vendor_number,
                "generated_vendor_number": proposed_vendor_number,
                "active": "on",
                "payment_terms_days": 30,
                "default_payment_method": Vendor.PaymentMethod.ACH,
            },
        )
        self.assertEqual(response.status_code, 302)
        generated_vendor = Vendor.objects.get(legal_name="Generated Vendor")
        self.assertEqual(generated_vendor.vendor_number, proposed_vendor_number)
        self.assertRegex(generated_vendor.vendor_number, r"^VND-\d{6}$")

        bill = self.create_bill(bill_number="")
        self.assertRegex(bill.bill_number, r"^B-\d{4}-\d{6}$")
        approved = self.submit_and_approve(bill)
        detail = self.client.get(reverse("accounting:bill_detail", args=[approved.pk]))
        self.assertContains(detail, "Generated payment numbers can be changed.")
        payment = record_payment_for_bill(
            bill_id=approved.pk,
            actor=self.creator,
            amount=approved.balance_due,
            payment_date=timezone.localdate(),
            method=Vendor.PaymentMethod.ACH,
            status=Payment.Status.SCHEDULED,
        )
        self.assertRegex(payment.payment_number, r"^P-\d{4}-\d{6}$")

    def test_manual_duplicate_reference_is_rejected_and_draft_records_can_be_edited(self):
        self.client.force_login(self.creator)
        self.client.post(
            reverse("accounting:vendor_create"),
            {
                "legal_name": "First Manual Vendor",
                "vendor_number": "MANUAL-1",
                "active": "on",
                "payment_terms_days": 30,
                "default_payment_method": Vendor.PaymentMethod.ACH,
            },
        )
        duplicate = self.client.post(
            reverse("accounting:vendor_create"),
            {
                "legal_name": "Duplicate Manual Vendor",
                "vendor_number": "MANUAL-1",
                "active": "on",
                "payment_terms_days": 30,
                "default_payment_method": Vendor.PaymentMethod.ACH,
            },
        )
        self.assertContains(duplicate, "already exists")

        draft = self.create_bill()
        edit = self.client.post(
            reverse("accounting:bill_edit", args=[draft.pk]),
            {
                "vendor": self.vendor.pk,
                "bill_number": "EDITED-1",
                "invoice_date": "2026-08-01",
                "received_date": "2026-08-02",
                "due_date": "2026-08-31",
                "currency": "USD",
                "description": "Updated",
                "tax": "0.00",
                "lines-TOTAL_FORMS": "1",
                "lines-INITIAL_FORMS": "1",
                "lines-MIN_NUM_FORMS": "1",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-id": draft.lines.get().pk,
                "lines-0-description": "Edited services",
                "lines-0-quantity": "1",
                "lines-0-unit_price": "200.00",
            },
        )
        self.assertRedirects(edit, reverse("accounting:bill_detail", args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(draft.bill_number, "EDITED-1")
        self.assertTrue(
            AccountingAuditLog.objects.filter(entity_id=draft.pk, action="bill_updated").exists()
        )

        self.submit_and_approve(draft)
        blocked = self.client.get(reverse("accounting:bill_edit", args=[draft.pk]))
        self.assertRedirects(blocked, reverse("accounting:bill_detail", args=[draft.pk]))

    def test_only_scheduled_payments_can_be_edited(self):
        bill = self.submit_and_approve(self.create_bill())
        scheduled = record_payment_for_bill(
            bill_id=bill.pk,
            actor=self.creator,
            amount=Decimal("25.00"),
            payment_date=timezone.localdate(),
            method=Vendor.PaymentMethod.ACH,
            status=Payment.Status.SCHEDULED,
        )
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("accounting:payment_edit", args=[scheduled.pk]),
            {
                "payment_number": "SCHEDULED-EDIT",
                "reference": "staff reference",
                "payment_date": "2026-08-12",
                "method": Vendor.PaymentMethod.CHECK,
                "amount": "20.00",
                "notes": "updated before processing",
            },
        )
        self.assertRedirects(response, reverse("accounting:payment_list"))
        scheduled.refresh_from_db()
        self.assertEqual(scheduled.payment_number, "SCHEDULED-EDIT")
        self.assertTrue(
            AccountingAuditLog.objects.filter(
                entity_id=scheduled.pk, action="payment_updated"
            ).exists()
        )

        processed = record_payment_for_bill(
            bill_id=bill.pk,
            actor=self.creator,
            amount=Decimal("25.00"),
            payment_date=timezone.localdate(),
            method=Vendor.PaymentMethod.ACH,
            status=Payment.Status.PROCESSED,
        )
        blocked = self.client.get(reverse("accounting:payment_edit", args=[processed.pk]))
        self.assertRedirects(blocked, reverse("accounting:payment_list"))
