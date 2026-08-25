# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Credentialing-platform staff maintain provider records and operational back-office work. Accounts payable staff need to enter vendor bills, have a different staff member approve them, and track payment records.

## Product Purpose

The platform organizes credentialing operations and staff-only administrative workflows. The accounts payable area records a controlled bill-to-payment workflow without moving money.

## Operating Context

Staff work in a desktop-oriented internal portal with Django authentication, staff navigation, and the Django admin.

## Capabilities and Constraints

Accounts payable is staff-only, requires a separate approver from the bill creator, keeps attachments private, and is tracking only: no bank transfer, payment-processor, or general-ledger integration.

## Evidence on Hand

Existing Django templates and staff navigation are the visual and interaction reference. No brand assets or external financial-system data are available.

## Product Principles

- Make approval state and outstanding amounts immediately legible.
- Keep sensitive vendor data out of routine lists and audit summaries.
- Preserve clear accountability for each staff action.
- Prefer familiar, efficient administrative workflows.
