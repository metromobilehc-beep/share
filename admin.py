from django.contrib import admin

from .models import (
    AccountingAuditLog,
    Bill,
    BillAttachment,
    BillIntake,
    BillLine,
    Payment,
    PaymentAllocation,
    Vendor,
    audit,
)
from .services import void_payment


class BillLineInline(admin.TabularInline):
    model = BillLine
    extra = 0


class BillAttachmentInline(admin.TabularInline):
    model = BillAttachment
    extra = 0
    readonly_fields = ("original_name", "uploaded_by", "uploaded_at")


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("vendor_number", "legal_name", "active", "masked_tax_id_display", "updated_at")
    list_filter = ("active", "default_payment_method")
    search_fields = ("vendor_number", "legal_name", "display_name", "contact_email")

    @admin.display(description="Tax ID")
    def masked_tax_id_display(self, obj):
        return obj.masked_tax_id


@admin.register(Bill)
class BillAdmin(admin.ModelAdmin):
    list_display = ("id", "vendor", "bill_number", "due_date", "total", "status", "created_by")
    list_filter = ("status", "currency", "vendor")
    search_fields = ("bill_number", "vendor__legal_name", "description")
    readonly_fields = (
        "subtotal", "total", "created_at", "updated_at", "submitted_at", "approved_at",
        "rejected_at", "voided_at",
    )
    inlines = (BillLineInline, BillAttachmentInline)
    actions = ("void_selected_bills",)

    @admin.action(description="Void selected draft/submitted bills")
    def void_selected_bills(self, request, queryset):
        eligible = queryset.filter(status__in=(Bill.Status.DRAFT, Bill.Status.SUBMITTED))
        count = 0
        for bill in eligible:
            bill.status = Bill.Status.VOID
            bill.voided_by = request.user
            bill.voided_at = __import__("django.utils.timezone", fromlist=["now"]).now()
            bill.save(update_fields=["status", "voided_by", "voided_at", "updated_at"])
            audit(bill, "bill_voided", request.user, f"Voided bill {bill.bill_number or bill.pk}.")
            count += 1
        self.message_user(request, f"Voided {count} eligible bill(s).")


class PaymentAllocationInline(admin.TabularInline):
    model = PaymentAllocation
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("payment_number", "vendor", "payment_date", "amount", "status", "created_by")
    list_filter = ("status", "method")
    search_fields = ("payment_number", "reference", "vendor__legal_name")
    readonly_fields = ("created_at", "processed_at", "voided_at")
    inlines = (PaymentAllocationInline,)
    actions = ("void_selected_payments",)

    @admin.action(description="Void selected non-void payments")
    def void_selected_payments(self, request, queryset):
        count = 0
        for payment in queryset.exclude(status=Payment.Status.VOID):
            void_payment(payment_id=payment.pk, actor=request.user)
            count += 1
        self.message_user(request, f"Voided {count} payment(s).")


@admin.register(BillAttachment)
class BillAttachmentAdmin(admin.ModelAdmin):
    list_display = ("original_name", "bill", "uploaded_by", "uploaded_at")
    readonly_fields = ("original_name", "uploaded_by", "uploaded_at")


@admin.register(BillIntake)
class BillIntakeAdmin(admin.ModelAdmin):
    list_display = ("id", "filename", "extraction_status", "submitted_by", "submitted_at", "resulting_bill")
    list_filter = ("extraction_status",)
    search_fields = ("filename", "submitted_by__username")
    exclude = ("original_file", "extraction_text", "extracted_data")
    readonly_fields = (
        "filename",
        "content_type",
        "extraction_status",
        "extraction_error",
        "submitted_by",
        "submitted_at",
        "resulting_bill",
    )


@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(admin.ModelAdmin):
    list_display = ("payment", "bill", "amount", "created_at")
    readonly_fields = ("payment", "bill", "amount", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AccountingAuditLog)
class AccountingAuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "entity_type", "entity_id", "action", "actor", "summary")
    list_filter = ("entity_type", "action")
    search_fields = ("summary",)
    readonly_fields = ("entity_type", "entity_id", "action", "actor", "timestamp", "summary")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
