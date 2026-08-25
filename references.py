import re

from django.apps import apps
from django.db import IntegrityError, transaction
from django.utils import timezone


REFERENCE_CONFIG = {
    "vendor": ("Vendor", "vendor_number", "VND-{number:06d}", None),
    "bill": ("Bill", "bill_number", "B-{year}-{number:06d}", "year"),
    "payment": ("Payment", "payment_number", "P-{year}-{number:06d}", "year"),
}


def _config(kind, year=None):
    model_name, field_name, template, uses_year = REFERENCE_CONFIG[kind]
    year = year or timezone.localdate().year
    return model_name, field_name, template, year if uses_year else 0


def _format(template, year, number):
    return template.format(year=year, number=number)


def propose_reference(kind, year=None):
    """Return the next readable reference without reserving it."""
    model_name, field_name, template, scope_year = _config(kind, year)
    model = apps.get_model("accounting", model_name)
    expression = re.escape(_format(template, scope_year, 0)).replace("000000", r"(\d+)")
    highest = 0
    for value in model.objects.exclude(**{field_name: ""}).values_list(field_name, flat=True):
        match = re.fullmatch(expression, value)
        if match:
            highest = max(highest, int(match.group(1)))
    return _format(template, scope_year, highest + 1)


def next_reference(kind, year=None):
    """Reserve a collision-safe reference for a record being created."""
    model_name, field_name, template, scope_year = _config(kind, year)
    model = apps.get_model("accounting", model_name)
    sequence_model = apps.get_model("accounting", "ReferenceSequence")

    for _ in range(3):
        try:
            with transaction.atomic():
                sequence, created = sequence_model.objects.get_or_create(
                    kind=kind,
                    year=scope_year,
                    defaults={"next_value": 1},
                )
                sequence = sequence_model.objects.select_for_update().get(pk=sequence.pk)
                candidate_number = sequence.next_value
                if created:
                    suggested = propose_reference(kind, scope_year)
                    candidate_number = int(suggested.rsplit("-", 1)[1])
                while model.objects.filter(**{field_name: _format(template, scope_year, candidate_number)}).exists():
                    candidate_number += 1
                sequence.next_value = candidate_number + 1
                sequence.save(update_fields=["next_value"])
                return _format(template, scope_year, candidate_number)
        except IntegrityError:
            continue
    raise IntegrityError("Unable to allocate a unique accounting reference. Please try again.")
