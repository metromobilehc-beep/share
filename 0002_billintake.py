# Generated manually for local bill intake.

import accounting.models
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BillIntake",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("original_file", models.FileField(storage=accounting.models.PrivateAccountingStorage(), upload_to=accounting.models.bill_intake_upload_path)),
                ("filename", models.CharField(max_length=255)),
                ("content_type", models.CharField(max_length=127)),
                ("extraction_text", models.TextField(blank=True)),
                ("extraction_status", models.CharField(choices=[("pending", "Pending"), ("extracted", "Extracted"), ("needs_review", "Needs review"), ("failed", "Failed")], default="pending", max_length=20)),
                ("extraction_error", models.CharField(blank=True, max_length=500)),
                ("extracted_data", models.JSONField(blank=True, default=dict)),
                ("submitted_at", models.DateTimeField(auto_now_add=True)),
                ("resulting_bill", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="source_intake", to="accounting.bill")),
                ("submitted_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="submitted_bill_intakes", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-submitted_at", "-pk"]},
        ),
    ]
