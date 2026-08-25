# Generated manually for editable accounting defaults.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0002_billintake"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReferenceSequence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(max_length=20)),
                ("year", models.PositiveIntegerField(default=0)),
                ("next_value", models.PositiveIntegerField(default=1)),
            ],
        ),
        migrations.AddConstraint(
            model_name="referencesequence",
            constraint=models.UniqueConstraint(
                fields=("kind", "year"), name="accounting_reference_sequence_kind_year_unique"
            ),
        ),
        migrations.AddField(
            model_name="billintake",
            name="reviewed_vendor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reviewed_bill_intakes",
                to="accounting.vendor",
            ),
        ),
    ]
