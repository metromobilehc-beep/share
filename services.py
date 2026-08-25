from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Bill, Payment, PaymentAllocation, audit


def refresh_bill_payment_status(bill):
    bill.refresh_from_db()
    if bill.status not in (Bill.Status.APPROVED, Bill.Status.PARTIALLY_PAID, Bill.Status.PAID):
        return bill
    if bill.balance_due == Decimal("0.00") and bill.total > 0:
        bill.status = Bill.Status.PAID
    elif bill.amount_paid > 0:
        bill.status = Bill.Status.PARTIALLY_PAID
    else:
        bill.status = Bill.Status.APPROVED
    bill.save(update_fields=["status", "updated_at"])
    return bill


@transaction.atomic
def record_payment_for_bill(
    *, bill_id, actor, amount, payment_date, method, status, notes="", reference="", payment_number=""
):
    bill = Bill.objects.select_for_update().select_related("vendor").get(pk=bill_id)
    if bill.status not in (Bill.Status.APPROVED, Bill.Status.PARTIALLY_PAID):
        raise ValidationError("A bill must be approved before a payment can be recorded.")
    if amount <= 0 or amount > bill.balance_due:
        raise ValidationError("Payment amount must be greater than zero and no more than the bill balance.")

    payment = Payment(
        vendor=bill.vendor,
        payment_date=payment_date,
        method=method,
        amount=amount,
        status=status,
        notes=notes,
        reference=reference,
        payment_number=payment_number,
        created_by=actor,
        processed_by=actor if status == Payment.Status.PROCESSED else None,
        processed_at=timezone.now() if status == Payment.Status.PROCESSED else None,
    )
    payment.full_clean()
    payment.save()
    if status == Payment.Status.PROCESSED:
        allocation = PaymentAllocation(payment=payment, bill=bill, amount=amount)
        allocation.full_clean()
        allocation.save()
        refresh_bill_payment_status(bill)
        audit(payment, "payment_recorded", actor, f"Recorded {payment.payment_number} for bill {bill.pk}.")
    else:
        audit(payment, "payment_scheduled", actor, f"Scheduled {payment.payment_number}; no allocation recorded.")
    return payment


@transaction.atomic
def void_payment(*, payment_id, actor):
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if payment.status == Payment.Status.VOID:
        raise ValidationError("This payment is already void.")
    affected_bill_ids = list(payment.allocations.values_list("bill_id", flat=True))
    payment.status = Payment.Status.VOID
    payment.voided_by = actor
    payment.voided_at = timezone.now()
    payment.save(update_fields=["status", "voided_by", "voided_at"])
    for bill_id in affected_bill_ids:
        refresh_bill_payment_status(Bill.objects.get(pk=bill_id))
    audit(payment, "payment_voided", actor, f"Voided {payment.payment_number}.")
    return payment
