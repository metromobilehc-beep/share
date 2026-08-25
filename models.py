from decimal import Decimal
from pathlib import Path
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.core.validators import RegexValidator
from django.db import IntegrityError, models, transaction
from django.db.models import Q, Sum
from django.utils import timezone


User = get_user_model()
vendor_number_validator = RegexValidator(
    regex=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$",
    message="Use letters, numbers, hyphens, or underscores (up to 32 characters).",
)
class PrivateAccountingStorage(FileSystemStorage):
    """Private, deployment-relative storage for staff-only bill attachments."""

    def __init__(self):
        super().__init__(location=Path(settings.BASE_DIR) / "private_accounting_attachments")

    def deconstruct(self):
        return "accounting.models.PrivateAccountingStorage", (), {}

    def url(self, name):
        raise ValueError("Accounting attachments use the staff download endpoint.")


private_attachment_storage = PrivateAccountingStorage()


class ReferenceSequence(models.Model):
    kind = models.CharField(max_length=20)
    year = models.PositiveIntegerField(default=0)
    next_value = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "year"], name="accounting_reference_sequence_kind_year_unique"
            )
        ]


class Vendor(models.Model):
    class PaymentMethod(models.TextChoices):
        ACH = "ach", "ACH"
        CHECK = "check", "Check"
        CARD = "card", "Card"
        WIRE = "wire", "Wire"
        OTHER = "other", "Other"

    legal_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    vendor_number = models.CharField(
        max_length=32, blank=True, validators=[vendor_number_validator]
    )
    tax_id = models.CharField(max_length=64, blank=True)
    remittance_address_line1 = models.CharField(max_length=255, blank=True)
    remittance_address_line2 = models.CharField(max_length=255, blank=True)
    remittance_city = models.CharField(max_length=100, blank=True)
    remittance_state = models.CharField(max_length=100, blank=True)
    remittance_postal_code = models.CharField(max_length=20, blank=True)
    remittance_country = models.CharField(max_length=100, blank=True, default="United States")
    contact_name = models.CharField(max_length=255, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)
    payment_terms_days = models.PositiveIntegerField(default=30)
    default_payment_method = models.CharField(
        max_length=10, choices=PaymentMethod.choices, default=PaymentMethod.ACH
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["legal_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["vendor_number"],
                condition=~Q(vendor_number=""),
                name="accounting_vendor_number_when_set_unique",
            )
        ]

    def __str__(self):
        return self.display_name or self.legal_name

    def save(self, *args, **kwargs):
        if self.pk is None and not self.vendor_number:
            from .references import next_reference

            for _ in range(3):
                try:
                    with transaction.atomic():
                        self.vendor_number = next_reference("vendor")
                        return super().save(*args, **kwargs)
                except IntegrityError:
                    self.vendor_number = ""
            raise IntegrityError("Unable to save vendor with a unique generated reference.")
        return super().save(*args, **kwargs)

    @property
    def masked_tax_id(self):
        if not self.tax_id:
            return ""
        return f"{'*' * max(len(self.tax_id) - 4, 0)}{self.tax_id[-4:]}"


class Bill(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        PAID = "paid", "Paid"
        VOID = "void", "Void"
        REJECTED = "rejected", "Rejected"

    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="bills")
    bill_number = models.CharField(max_length=100, blank=True)
    invoice_date = models.DateField()
    received_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    description = models.TextField(blank=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_bills")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="approved_bills"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="rejected_bills"
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    voided_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="voided_bills"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["due_date", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["vendor", "bill_number"],
                condition=~Q(bill_number=""),
                name="accounting_vendor_bill_number_when_set_unique",
            )
        ]

    def __str__(self):
        return f"{self.vendor} · {self.bill_number or f'Bill {self.pk}'}"

    def save(self, *args, **kwargs):
        if self.pk and self.lines.exists():
            subtotal = sum((line.line_total for line in self.lines.all()), Decimal("0.00"))
            self.subtotal = subtotal.quantize(Decimal("0.01"))
            self.total = (self.subtotal + self.tax).quantize(Decimal("0.01"))
        if self.pk is None and not self.bill_number:
            from .references import next_reference

            for _ in range(3):
                try:
                    with transaction.atomic():
                        self.bill_number = next_reference("bill")
                        return super().save(*args, **kwargs)
                except IntegrityError:
                    self.bill_number = ""
            raise IntegrityError("Unable to save bill with a unique generated reference.")
        return super().save(*args, **kwargs)

    @property
    def amount_paid(self):
        return (
            self.allocations.filter(payment__status=Payment.Status.PROCESSED).aggregate(
                amount=Sum("amount")
            )["amount"]
            or Decimal("0.00")
        )

    @property
    def balance_due(self):
        return max(self.total - self.amount_paid, Decimal("0.00"))

    def recalculate_totals(self):
        subtotal = sum(
            (line.line_total for line in self.lines.all()),
            Decimal("0.00"),
        )
        self.subtotal = subtotal.quantize(Decimal("0.01"))
        self.total = (self.subtotal + self.tax).quantize(Decimal("0.01"))
        type(self).objects.filter(pk=self.pk).update(
            subtotal=self.subtotal, total=self.total, updated_at=timezone.now()
        )


class BillLine(models.Model):
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1.00"))
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    taxable = models.BooleanField(default=False)

    class Meta:
        ordering = ["pk"]

    @property
    def line_total(self):
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"))

    def clean(self):
        if self.quantity <= 0:
            raise ValidationError({"quantity": "Quantity must be greater than zero."})
        if self.unit_price < 0:
            raise ValidationError({"unit_price": "Unit price cannot be negative."})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.bill.recalculate_totals()

    def delete(self, *args, **kwargs):
        bill = self.bill
        super().delete(*args, **kwargs)
        bill.recalculate_totals()


class Payment(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        PROCESSED = "processed", "Processed"
        VOID = "void", "Void"

    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="payments")
    payment_number = models.CharField(max_length=32, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    payment_date = models.DateField(default=timezone.localdate)
    method = models.CharField(max_length=10, choices=Vendor.PaymentMethod.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PROCESSED)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_payments")
    created_at = models.DateTimeField(auto_now_add=True)
    processed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="processed_payments"
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="voided_payments"
    )
    voided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-payment_date", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment_number"],
                condition=~Q(payment_number=""),
                name="accounting_payment_number_when_set_unique",
            )
        ]

    def __str__(self):
        return self.payment_number or f"Payment {self.pk}"

    def clean(self):
        if self.amount <= 0:
            raise ValidationError({"amount": "Payment amount must be greater than zero."})

    def save(self, *args, **kwargs):
        if self.pk is None and not self.payment_number:
            from .references import next_reference

            for _ in range(3):
                try:
                    with transaction.atomic():
                        self.payment_number = next_reference("payment")
                        return super().save(*args, **kwargs)
                except IntegrityError:
                    self.payment_number = ""
            raise IntegrityError("Unable to save payment with a unique generated reference.")
        return super().save(*args, **kwargs)

    @property
    def allocated_amount(self):
        if self.status != self.Status.PROCESSED:
            return Decimal("0.00")
        return self.allocations.aggregate(amount=Sum("amount"))["amount"] or Decimal("0.00")

    @property
    def available_amount(self):
        return max(self.amount - self.allocated_amount, Decimal("0.00"))


class PaymentAllocation(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="allocations")
    bill = models.ForeignKey(Bill, on_delete=models.PROTECT, related_name="allocations")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "bill"], name="accounting_one_allocation_per_bill_payment"
            )
        ]

    def clean(self):
        errors = {}
        if self.amount <= 0:
            errors["amount"] = "Allocation amount must be greater than zero."
        if self.payment.vendor_id != self.bill.vendor_id:
            errors["bill"] = "A payment can only be allocated to the same vendor."
        if self.payment.status != Payment.Status.PROCESSED:
            errors["payment"] = "Only processed payments can be allocated."
        if self.bill.status not in (Bill.Status.APPROVED, Bill.Status.PARTIALLY_PAID):
            errors["bill"] = "Only approved bills can receive payments."
        if self.pk:
            existing = type(self).objects.get(pk=self.pk)
            paid_before = self.bill.amount_paid - existing.amount
            allocated_before = self.payment.allocated_amount - existing.amount
        else:
            paid_before = self.bill.amount_paid
            allocated_before = self.payment.allocated_amount
        if self.amount > max(self.bill.total - paid_before, Decimal("0.00")):
            errors["amount"] = "Allocation exceeds the bill balance."
        if self.amount > max(self.payment.amount - allocated_before, Decimal("0.00")):
            errors["amount"] = "Allocation exceeds the unallocated payment amount."
        if errors:
            raise ValidationError(errors)


def attachment_upload_path(instance, filename):
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)
    return f"bills/{instance.bill_id}/{safe_name}"


def bill_intake_upload_path(instance, filename):
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)
    return f"bill-intakes/{safe_name}"


class BillAttachment(models.Model):
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_upload_path, storage=private_attachment_storage)
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.original_name


class BillIntake(models.Model):
    class ExtractionStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        EXTRACTED = "extracted", "Extracted"
        NEEDS_REVIEW = "needs_review", "Needs review"
        FAILED = "failed", "Failed"

    original_file = models.FileField(
        upload_to=bill_intake_upload_path,
        storage=private_attachment_storage,
    )
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=127)
    extraction_text = models.TextField(blank=True)
    extraction_status = models.CharField(
        max_length=20,
        choices=ExtractionStatus.choices,
        default=ExtractionStatus.PENDING,
    )
    extraction_error = models.CharField(max_length=500, blank=True)
    extracted_data = models.JSONField(default=dict, blank=True)
    submitted_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="submitted_bill_intakes",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    resulting_bill = models.OneToOneField(
        Bill,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="source_intake",
    )
    reviewed_vendor = models.ForeignKey(
        Vendor,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_bill_intakes",
    )

    class Meta:
        ordering = ["-submitted_at", "-pk"]

    def __str__(self):
        return f"Bill intake {self.pk}: {self.filename}"


class AccountingAuditLog(models.Model):
    entity_type = models.CharField(max_length=40)
    entity_id = models.PositiveBigIntegerField()
    action = models.CharField(max_length=80)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    timestamp = models.DateTimeField(auto_now_add=True)
    summary = models.CharField(max_length=500)

    class Meta:
        ordering = ["-timestamp", "-pk"]

    def __str__(self):
        return f"{self.entity_type} {self.entity_id}: {self.action}"


def audit(entity, action, actor, summary):
    """Record concise operational history without copying sensitive fields."""
    return AccountingAuditLog.objects.create(
        entity_type=entity._meta.model_name,
        entity_id=entity.pk,
        action=action,
        actor=actor,
        summary=summary[:500],
    )
