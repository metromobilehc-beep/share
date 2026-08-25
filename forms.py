from decimal import Decimal

from django import forms
from django.forms import inlineformset_factory
from django.utils import timezone

from .models import Bill, BillAttachment, BillIntake, BillLine, Payment, Vendor
from .references import propose_reference


class VendorForm(forms.ModelForm):
    generated_vendor_number = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Vendor
        exclude = ("created_at", "updated_at")
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, generated_reference=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            generated_reference = generated_reference or propose_reference("vendor")
            self.fields["vendor_number"].initial = generated_reference
            self.fields["vendor_number"].help_text = "Generated reference; staff may replace it."
            self.fields["generated_vendor_number"].initial = generated_reference

    def clean_vendor_number(self):
        value = self.cleaned_data["vendor_number"].strip()
        if value and value == self.data.get("generated_vendor_number", ""):
            return ""
        if value and Vendor.objects.filter(vendor_number=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A vendor with this vendor number already exists.")
        return value


class BillForm(forms.ModelForm):
    generated_bill_number = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Bill
        fields = (
            "vendor",
            "bill_number",
            "invoice_date",
            "received_date",
            "due_date",
            "currency",
            "description",
            "tax",
        )
        widgets = {
            "invoice_date": forms.DateInput(attrs={"type": "date"}),
            "received_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, generated_reference=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk and not self.initial.get("bill_number"):
            generated_reference = generated_reference or propose_reference("bill")
            self.fields["bill_number"].initial = generated_reference
            self.fields["bill_number"].help_text = "Generated reference; staff may replace it."
            self.fields["generated_bill_number"].initial = generated_reference

    def clean_bill_number(self):
        value = self.cleaned_data["bill_number"].strip()
        if value and value == self.data.get("generated_bill_number", ""):
            return ""
        return value

    def clean_tax(self):
        tax = self.cleaned_data["tax"]
        if tax < 0:
            raise forms.ValidationError("Tax cannot be negative.")
        return tax

    def clean(self):
        cleaned = super().clean()
        vendor = cleaned.get("vendor")
        bill_number = cleaned.get("bill_number", "").strip()
        if vendor and bill_number and Bill.objects.filter(
            vendor=vendor, bill_number=bill_number
        ).exclude(pk=self.instance.pk).exists():
            self.add_error(
                "bill_number",
                "A bill with this vendor and bill number already exists.",
            )
        return cleaned


BillLineFormSet = inlineformset_factory(
    Bill,
    BillLine,
    fields=("description", "quantity", "unit_price", "taxable"),
    extra=1,
    min_num=1,
    validate_min=True,
    can_delete=True,
)


class BillIntakeUploadForm(forms.ModelForm):
    max_file_size = 15 * 1024 * 1024
    allowed_types = {
        ".pdf": {"application/pdf"},
        ".docx": {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
        ".png": {"image/png"},
        ".jpg": {"image/jpeg"},
        ".jpeg": {"image/jpeg"},
    }

    class Meta:
        model = BillIntake
        fields = ("original_file",)

    def clean_original_file(self):
        uploaded = self.cleaned_data["original_file"]
        suffix = __import__("pathlib").Path(uploaded.name).suffix.lower()
        if suffix not in self.allowed_types:
            raise forms.ValidationError("Upload a PDF, DOCX, PNG, or JPEG bill.")
        content_type = (uploaded.content_type or "").lower()
        if content_type not in self.allowed_types[suffix]:
            raise forms.ValidationError("The file content type does not match an allowed bill format.")
        if uploaded.size > self.max_file_size:
            raise forms.ValidationError("Bills must be 15 MB or smaller.")
        return uploaded


class RejectBillForm(forms.Form):
    rejection_reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=1000)


class PaymentForm(forms.Form):
    bill = forms.ModelChoiceField(queryset=Bill.objects.none(), widget=forms.HiddenInput)
    payment_number = forms.CharField(max_length=32, required=False)
    generated_payment_number = forms.CharField(required=False, widget=forms.HiddenInput)
    payment_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    method = forms.ChoiceField(choices=Vendor.PaymentMethod.choices)
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    status = forms.ChoiceField(choices=Payment.Status.choices, initial=Payment.Status.PROCESSED)
    reference = forms.CharField(max_length=100, required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, bill=None, generated_reference=None, **kwargs):
        super().__init__(*args, **kwargs)
        eligible = Bill.objects.filter(
            status__in=(Bill.Status.APPROVED, Bill.Status.PARTIALLY_PAID)
        )
        self.fields["bill"].queryset = eligible
        if bill:
            self.fields["bill"].initial = bill
            self.fields["amount"].initial = bill.balance_due
            self.fields["method"].initial = bill.vendor.default_payment_method
            self.fields["payment_date"].initial = timezone.localdate()
            generated_reference = generated_reference or propose_reference("payment")
            self.fields["payment_number"].initial = generated_reference
            self.fields["payment_number"].help_text = "Generated reference; staff may replace it."
            self.fields["generated_payment_number"].initial = generated_reference

    def clean_payment_number(self):
        value = self.cleaned_data["payment_number"].strip()
        if value and value == self.data.get("generated_payment_number", ""):
            return ""
        if value and Payment.objects.filter(payment_number=value).exists():
            raise forms.ValidationError("A payment with this payment number already exists.")
        return value

    def clean(self):
        cleaned = super().clean()
        bill = cleaned.get("bill")
        amount = cleaned.get("amount")
        if bill and amount and amount > bill.balance_due:
            self.add_error("amount", "Amount cannot exceed the current bill balance.")
        return cleaned


class PaymentEditForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ("payment_number", "reference", "payment_date", "method", "amount", "notes")
        widgets = {
            "payment_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def clean_payment_number(self):
        value = self.cleaned_data["payment_number"].strip()
        if value and Payment.objects.filter(payment_number=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A payment with this payment number already exists.")
        return value


class BillAttachmentForm(forms.ModelForm):
    class Meta:
        model = BillAttachment
        fields = ("file",)

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if uploaded.size > 10 * 1024 * 1024:
            raise forms.ValidationError("Attachments must be 10 MB or smaller.")
        return uploaded
