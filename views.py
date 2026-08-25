from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.files import File
from django.db import transaction
from django.db.models import Sum
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    BillAttachmentForm,
    BillForm,
    BillIntakeUploadForm,
    BillLineFormSet,
    PaymentEditForm,
    PaymentForm,
    RejectBillForm,
    VendorForm,
)
from .intake import LocalExtractionError, OCRUnavailable, extract_document_text, normalize_vendor_name, parse_invoice_text
from .models import Bill, BillAttachment, BillIntake, Payment, Vendor, audit
from .references import propose_reference
from .services import record_payment_for_bill, void_payment


def _money_sum(bills):
    return sum((bill.balance_due for bill in bills), Decimal("0.00"))


@staff_member_required
def dashboard(request):
    today = timezone.localdate()
    awaiting = Bill.objects.filter(status=Bill.Status.SUBMITTED)
    drafts = Bill.objects.filter(status=Bill.Status.DRAFT)
    approved = Bill.objects.filter(status__in=(Bill.Status.APPROVED, Bill.Status.PARTIALLY_PAID))
    due_7 = approved.filter(due_date__gte=today, due_date__lte=today + timedelta(days=7))
    due_30 = approved.filter(due_date__gte=today, due_date__lte=today + timedelta(days=30))
    overdue = approved.filter(due_date__lt=today)
    return render(
        request,
        "accounting/dashboard.html",
        {
            "draft_count": drafts.count(),
            "draft_total": _money_sum(drafts),
            "awaiting_count": awaiting.count(),
            "awaiting_total": _money_sum(awaiting),
            "due_7": _money_sum(due_7),
            "due_30": _money_sum(due_30),
            "overdue": _money_sum(overdue),
            "approved_unpaid": _money_sum(approved),
        },
    )


@staff_member_required
def vendor_list(request):
    return render(request, "accounting/vendor_list.html", {"vendors": Vendor.objects.all()})


@staff_member_required
def vendor_create(request):
    form = VendorForm(request.POST or None, generated_reference=propose_reference("vendor"))
    if request.method == "POST" and form.is_valid():
        vendor = form.save()
        audit(vendor, "vendor_created", request.user, f"Created vendor {vendor.vendor_number}.")
        messages.success(request, "Vendor created.")
        return redirect("accounting:vendor_list")
    return render(request, "accounting/vendor_form.html", {"form": form, "is_edit": False})


@staff_member_required
def vendor_edit(request, vendor_id):
    vendor = get_object_or_404(Vendor, pk=vendor_id)
    form = VendorForm(request.POST or None, instance=vendor)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(vendor, "vendor_updated", request.user, f"Updated vendor {vendor.vendor_number}.")
        messages.success(request, "Vendor updated.")
        return redirect("accounting:vendor_list")
    return render(request, "accounting/vendor_form.html", {"form": form, "is_edit": True, "vendor": vendor})


@staff_member_required
def bill_list(request):
    bills = Bill.objects.select_related("vendor", "created_by").all()
    status = request.GET.get("status", "")
    vendor = request.GET.get("vendor", "")
    due = request.GET.get("due", "")
    today = timezone.localdate()
    if status:
        bills = bills.filter(status=status)
    if vendor:
        bills = bills.filter(vendor_id=vendor)
    if due == "overdue":
        bills = bills.filter(due_date__lt=today)
    elif due == "7":
        bills = bills.filter(due_date__gte=today, due_date__lte=today + timedelta(days=7))
    elif due == "30":
        bills = bills.filter(due_date__gte=today, due_date__lte=today + timedelta(days=30))
    return render(
        request,
        "accounting/bill_list.html",
        {"bills": bills, "vendors": Vendor.objects.filter(active=True), "status_choices": Bill.Status.choices},
    )


@staff_member_required
def bill_create(request):
    bill = Bill(created_by=request.user)
    form = BillForm(request.POST or None, instance=bill, generated_reference=propose_reference("bill"))
    formset = BillLineFormSet(request.POST or None, instance=bill)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        bill = form.save(commit=False)
        bill.created_by = request.user
        bill.save()
        formset.instance = bill
        formset.save()
        bill.recalculate_totals()
        audit(bill, "bill_created", request.user, f"Created bill {bill.bill_number or bill.pk}.")
        messages.success(request, "Bill saved as draft.")
        return redirect("accounting:bill_detail", bill_id=bill.pk)
    return render(
        request, "accounting/bill_form.html", {"form": form, "formset": formset, "is_edit": False}
    )


@staff_member_required
def bill_edit(request, bill_id):
    bill = get_object_or_404(Bill, pk=bill_id)
    if bill.status != Bill.Status.DRAFT:
        messages.error(request, "Only draft bills can be edited. Approval history is unchanged.")
        return redirect("accounting:bill_detail", bill_id=bill.pk)
    form = BillForm(request.POST or None, instance=bill)
    formset = BillLineFormSet(request.POST or None, instance=bill)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            form.save()
            formset.save()
            bill.recalculate_totals()
            audit(bill, "bill_updated", request.user, f"Updated draft bill {bill.bill_number}.")
        messages.success(request, "Draft bill updated.")
        return redirect("accounting:bill_detail", bill_id=bill.pk)
    return render(
        request, "accounting/bill_form.html", {"form": form, "formset": formset, "is_edit": True, "bill": bill}
    )


def _matching_vendor(name):
    normalized = normalize_vendor_name(name or "")
    if not normalized:
        return None
    matches = []
    for vendor in Vendor.objects.filter(active=True).only("id", "legal_name", "display_name"):
        if normalized in {
            normalize_vendor_name(vendor.legal_name),
            normalize_vendor_name(vendor.display_name),
        }:
            matches.append(vendor)
    return matches[0] if len(matches) == 1 else None


@staff_member_required
def bill_intake_upload(request):
    form = BillIntakeUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        intake = form.save(commit=False)
        uploaded = form.cleaned_data["original_file"]
        intake.filename = Path(uploaded.name).name[:255]
        intake.content_type = uploaded.content_type
        intake.submitted_by = request.user
        intake.save()
        try:
            with intake.original_file.open("rb") as source:
                text = extract_document_text(source, intake.filename)
        except OCRUnavailable as error:
            intake.extraction_status = BillIntake.ExtractionStatus.NEEDS_REVIEW
            intake.extraction_error = str(error)
        except LocalExtractionError as error:
            intake.extraction_status = BillIntake.ExtractionStatus.FAILED
            intake.extraction_error = str(error)
        else:
            intake.extraction_text = text
            intake.extracted_data = parse_invoice_text(text)
            if text.strip():
                intake.extraction_status = BillIntake.ExtractionStatus.EXTRACTED
            else:
                intake.extraction_status = BillIntake.ExtractionStatus.NEEDS_REVIEW
                intake.extraction_error = "No readable text was found; enter the bill details manually."
        intake.save(
            update_fields=["extraction_text", "extracted_data", "extraction_status", "extraction_error"]
        )
        return redirect("accounting:bill_intake_review", intake_id=intake.pk)
    return render(request, "accounting/bill_intake_upload.html", {"form": form})


@staff_member_required
def bill_intake_review(request, intake_id):
    intake = get_object_or_404(BillIntake, pk=intake_id)
    if intake.resulting_bill_id:
        return redirect("accounting:bill_detail", bill_id=intake.resulting_bill_id)
    data = intake.extracted_data or {}
    matched_vendor = intake.reviewed_vendor or _matching_vendor(data.get("vendor_name"))
    initial = {
        "vendor": matched_vendor,
        "bill_number": data.get("bill_number", ""),
        "invoice_date": data.get("invoice_date", ""),
        "received_date": timezone.localdate(),
        "due_date": data.get("due_date", ""),
        "tax": data.get("tax", "") or "0.00",
        "description": "",
    }
    bill = Bill(created_by=request.user)
    generated_reference = None if initial["bill_number"] else propose_reference("bill")
    form = BillForm(
        request.POST or None,
        instance=bill,
        initial=initial,
        generated_reference=generated_reference,
    )
    formset = BillLineFormSet(
        request.POST or None,
        instance=bill,
        initial=data.get("line_items", []),
    )
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            bill = form.save(commit=False)
            bill.created_by = request.user
            bill.status = Bill.Status.DRAFT
            bill.save()
            formset.instance = bill
            formset.save()
            bill.recalculate_totals()
            with intake.original_file.open("rb") as source:
                attachment = BillAttachment(
                    bill=bill,
                    original_name=intake.filename,
                    uploaded_by=request.user,
                )
                attachment.file.save(intake.filename, File(source), save=False)
                attachment.save()
            intake.resulting_bill = bill
            intake.save(update_fields=["resulting_bill"])
            audit(bill, "bill_imported", request.user, "Created draft bill from staff-reviewed upload.")
        messages.success(request, "Draft bill created from the reviewed upload.")
        return redirect("accounting:bill_detail", bill_id=bill.pk)
    return render(
        request,
        "accounting/bill_intake_review.html",
        {
            "intake": intake,
            "form": form,
            "formset": formset,
            "matched_vendor": matched_vendor,
            "detected_total": data.get("total", ""),
            "detected_subtotal": data.get("subtotal", ""),
            "vendor_draft_form": VendorForm(
                generated_reference=propose_reference("vendor"),
                initial={"legal_name": data.get("vendor_name", ""), "display_name": data.get("vendor_name", "")},
            )
            if data.get("vendor_name") and not matched_vendor
            else None,
        },
    )


@staff_member_required
def bill_intake_vendor_draft(request, intake_id):
    intake = get_object_or_404(BillIntake, pk=intake_id)
    if intake.resulting_bill_id:
        return redirect("accounting:bill_detail", bill_id=intake.resulting_bill_id)
    if intake.reviewed_vendor_id:
        return redirect("accounting:bill_intake_review", intake_id=intake.pk)
    form = VendorForm(request.POST or None, generated_reference=propose_reference("vendor"))
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            vendor = form.save()
            intake.reviewed_vendor = vendor
            intake.save(update_fields=["reviewed_vendor"])
            audit(vendor, "vendor_created_from_intake", request.user, "Created vendor from staff-reviewed bill intake.")
        messages.success(request, "Vendor saved. Confirm the selected vendor and bill details below.")
    else:
        messages.error(request, "Vendor draft could not be saved. Review the highlighted fields.")
    return redirect("accounting:bill_intake_review", intake_id=intake.pk)


@staff_member_required
def bill_intake_original_download(request, intake_id):
    intake = get_object_or_404(BillIntake, pk=intake_id)
    if not intake.original_file:
        raise Http404
    return FileResponse(
        intake.original_file.open("rb"),
        as_attachment=True,
        filename=intake.filename,
    )


@staff_member_required
def bill_detail(request, bill_id):
    bill = get_object_or_404(
        Bill.objects.select_related("vendor", "created_by", "approved_by", "rejected_by"), pk=bill_id
    )
    return render(
        request,
        "accounting/bill_detail.html",
        {
            "bill": bill,
            "reject_form": RejectBillForm(),
            "attachment_form": BillAttachmentForm(),
            "payment_form": PaymentForm(bill=bill, generated_reference=propose_reference("payment"))
            if bill.status in (Bill.Status.APPROVED, Bill.Status.PARTIALLY_PAID)
            else None,
        },
    )


@staff_member_required
@require_POST
def bill_submit(request, bill_id):
    bill = get_object_or_404(Bill, pk=bill_id)
    if bill.status != Bill.Status.DRAFT:
        messages.error(request, "Only draft bills can be submitted.")
    elif not bill.lines.exists() or bill.total <= 0:
        messages.error(request, "Add at least one line with a positive total before submitting.")
    else:
        bill.status = Bill.Status.SUBMITTED
        bill.submitted_at = timezone.now()
        bill.save(update_fields=["status", "submitted_at", "updated_at"])
        audit(bill, "bill_submitted", request.user, f"Submitted bill {bill.bill_number or bill.pk}.")
        messages.success(request, "Bill submitted for approval.")
    return redirect("accounting:bill_detail", bill_id=bill.pk)


@staff_member_required
@require_POST
def bill_approve(request, bill_id):
    bill = get_object_or_404(Bill, pk=bill_id)
    if bill.status != Bill.Status.SUBMITTED:
        messages.error(request, "Only submitted bills can be approved.")
    elif bill.created_by_id == request.user.id:
        messages.error(request, "The bill creator cannot approve this bill.")
    else:
        bill.status = Bill.Status.APPROVED
        bill.approved_by = request.user
        bill.approved_at = timezone.now()
        bill.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        audit(bill, "bill_approved", request.user, f"Approved bill {bill.bill_number or bill.pk}.")
        messages.success(request, "Bill approved.")
    return redirect("accounting:bill_detail", bill_id=bill.pk)


@staff_member_required
@require_POST
def bill_reject(request, bill_id):
    bill = get_object_or_404(Bill, pk=bill_id)
    form = RejectBillForm(request.POST)
    if bill.status != Bill.Status.SUBMITTED:
        messages.error(request, "Only submitted bills can be rejected.")
    elif bill.created_by_id == request.user.id:
        messages.error(request, "The bill creator cannot reject their own bill.")
    elif form.is_valid():
        bill.status = Bill.Status.REJECTED
        bill.rejected_by = request.user
        bill.rejected_at = timezone.now()
        bill.rejection_reason = form.cleaned_data["rejection_reason"]
        bill.save(
            update_fields=[
                "status", "rejected_by", "rejected_at", "rejection_reason", "updated_at"
            ]
        )
        audit(bill, "bill_rejected", request.user, f"Rejected bill {bill.bill_number or bill.pk}.")
        messages.success(request, "Bill rejected.")
    else:
        messages.error(request, "A rejection reason is required.")
    return redirect("accounting:bill_detail", bill_id=bill.pk)


@staff_member_required
@require_POST
def bill_attachment_upload(request, bill_id):
    bill = get_object_or_404(Bill, pk=bill_id)
    form = BillAttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        attachment = form.save(commit=False)
        attachment.bill = bill
        attachment.original_name = attachment.file.name
        attachment.uploaded_by = request.user
        attachment.save()
        audit(bill, "attachment_uploaded", request.user, f"Uploaded attachment {attachment.original_name}.")
        messages.success(request, "Attachment uploaded.")
    else:
        messages.error(request, "Attachment could not be uploaded.")
    return redirect("accounting:bill_detail", bill_id=bill.pk)


@staff_member_required
def attachment_download(request, attachment_id):
    attachment = get_object_or_404(BillAttachment, pk=attachment_id)
    if not attachment.file:
        raise Http404
    return FileResponse(
        attachment.file.open("rb"),
        as_attachment=True,
        filename=attachment.original_name,
    )


@staff_member_required
@require_POST
def payment_create(request):
    bill_id = request.POST.get("bill")
    bill = get_object_or_404(Bill, pk=bill_id)
    form = PaymentForm(request.POST, bill=bill)
    if form.is_valid():
        try:
            payment = record_payment_for_bill(
                bill_id=form.cleaned_data["bill"].pk,
                actor=request.user,
                amount=form.cleaned_data["amount"],
                payment_date=form.cleaned_data["payment_date"],
                method=form.cleaned_data["method"],
                status=form.cleaned_data["status"],
                notes=form.cleaned_data["notes"],
                reference=form.cleaned_data["reference"],
                payment_number=form.cleaned_data["payment_number"],
            )
        except Exception as error:
            messages.error(request, str(error))
        else:
            messages.success(
                request,
                "Payment recorded and applied." if payment.status == Payment.Status.PROCESSED
                else "Payment scheduled; it has not been applied to the bill.",
            )
    else:
        errors = " ".join(
            message for field_errors in form.errors.values() for message in field_errors
        )
        messages.error(
            request,
            errors or "Payment could not be recorded. Check the amount and bill status.",
        )
    return redirect("accounting:bill_detail", bill_id=bill.pk)


@staff_member_required
def payment_list(request):
    return render(
        request,
        "accounting/payment_list.html",
        {"payments": Payment.objects.select_related("vendor", "created_by").all()},
    )


@staff_member_required
def payment_edit(request, payment_id):
    payment = get_object_or_404(Payment, pk=payment_id)
    if payment.status != Payment.Status.SCHEDULED:
        messages.error(request, "Only scheduled payments can be edited. Processed payments can only be voided.")
        return redirect("accounting:payment_list")
    form = PaymentEditForm(request.POST or None, instance=payment)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(payment, "payment_updated", request.user, f"Updated scheduled payment {payment.payment_number}.")
        messages.success(request, "Scheduled payment updated.")
        return redirect("accounting:payment_list")
    return render(request, "accounting/payment_form.html", {"form": form, "payment": payment})


@staff_member_required
@require_POST
def payment_void(request, payment_id):
    try:
        void_payment(payment_id=payment_id, actor=request.user)
        messages.success(request, "Payment voided. Related bill balances were recalculated.")
    except Exception as error:
        messages.error(request, str(error))
    return redirect("accounting:payment_list")


@staff_member_required
def aging_report(request):
    today = timezone.localdate()
    bills = Bill.objects.filter(
        status__in=(Bill.Status.APPROVED, Bill.Status.PARTIALLY_PAID)
    ).select_related("vendor")
    bands = {"current": [], "1-30": [], "31-60": [], "61-90": [], "91+": []}
    for bill in bills:
        days_overdue = (today - bill.due_date).days
        if days_overdue <= 0:
            bands["current"].append(bill)
        elif days_overdue <= 30:
            bands["1-30"].append(bill)
        elif days_overdue <= 60:
            bands["31-60"].append(bill)
        elif days_overdue <= 90:
            bands["61-90"].append(bill)
        else:
            bands["91+"].append(bill)
    rows = [
        {"label": label, "bills": value, "total": _money_sum(value)}
        for label, value in bands.items()
    ]
    return render(request, "accounting/aging_report.html", {"rows": rows})
