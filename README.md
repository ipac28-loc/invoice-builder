# Indico API Invoice PDF Generator

This command-line tool retrieves registrations directly from Indico 3.3 and creates two independent English PDF documents:

- `Invoice_<First name>_<Family name>.pdf`
- `Payment_Certificate_<First name>_<Family name>.pdf` (only for a paid registration)

No CSV export is required. The invoice is a normal invoice; payment is evidenced separately by the payment certificate.

## How it accesses Indico

The tool uses Indico's check-in API:

```text
GET /api/checkin/event/{event_id}/forms/
GET /api/checkin/event/{event_id}/forms/{form_id}/registrations/
GET /api/checkin/event/{event_id}/forms/{form_id}/registrations/{registration_id}
```

The list response does not include custom registration answers, so the tool retrieves each registration's detail record. Detail requests run in parallel, with the limit controlled by `parallel_requests`.

The Indico API currently classifies these check-in endpoints as undocumented. They are used by Indico's check-in application but may change in a future Indico release. Test this tool after every Indico upgrade.

## 1. Recommended registration-form questions

Add these questions to the Indico registration form:

- `Invoice Requested` (Yes/No checkbox)
- `Invoice Addressee` (text)
- `Invoice Address` (multiline text)

The question titles are mapped under `api_fields` in `config.json`; they may be changed there without modifying Python code.

## 2. Create an Indico personal API token

Create a personal API token from the Indico user profile. Grant it the `registrants` scope. The owner of the token must also have registration-management or check-in permission for the event.

Store the token only in an environment variable:

```bash
export INDICO_API_TOKEN='indp_REPLACE_WITH_THE_REAL_TOKEN'
```

Do not put the token in `config.json`, Git, shell history, or the generated PDFs. For a service, place it in a root-readable environment file and load it from systemd.

## 3. Installation (AlmaLinux 10)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## 4. Configure the event

Copy the public example configuration to a local operational file, then edit `config.json`:

```bash
cp config.example.json config.json
```

`config.example.json` contains fictional examples. The local `config.json` and the default `output/` directory are excluded from Git.

Configure the connection:

```json
"indico": {
  "base_url": "https://indico.example.org",
  "event_id": 123,
  "registration_form_id": 456,
  "token_env": "INDICO_API_TOKEN",
  "verify_tls": true,
  "timeout_seconds": 30,
  "parallel_requests": 8
}
```

Replace `event_id` with the event ID from its Indico URL.

To find the registration-form ID through the API, set the event ID and run:

```bash
python generate_documents.py --config config.json --list-forms
```

Then put the displayed form ID in `registration_form_id`.

Also replace the sample issuer name, postal address, and email. Add a tax-registration number only if applicable. Confirm tax treatment with the conference accounting officer.

### Override payment-certificate billing details

Use the optional top-level `receipt_overrides` object in `config.json` to customize a payment certificate for a specific registration. Each key must be the registration ID as a JSON string:

```json
"receipt_overrides": {
  "1028": {
    "billing_name": "Example Research Institute",
    "billing_address": "1 Example Street\nExample City\nExample Country",
    "vat_number": "EXAMPLE-VAT-NUMBER"
  }
}
```

- `billing_name` replaces the certificate's `Receipt issued to` value.
- `billing_address` replaces its `Billing address`; use `\n` for line breaks.
- `vat_number` adds a `VAT number` row when nonempty. This is the recipient's VAT number, separate from `issuer.tax_registration_number`.

Omitted billing fields retain their registration values; if no billing name is available, the participant's name is used. An explicitly empty string clears the corresponding value, and an empty billing address or VAT number is omitted from the certificate. VAT numbers are read only from `receipt_overrides`.

These overrides affect payment certificates only. Invoices continue to use the registration's billing details, and participant names and PDF filenames are unchanged. Overrides do not update Indico or change payment status: a certificate is still generated only for a paid registration. Remove an entry to restore the registration's billing details, or omit `receipt_overrides` entirely when no overrides are needed.

## 5. Generate PDFs

Generate documents for all active registrations in the configured form:

```bash
python generate_documents.py \
  --config config.json \
  --output-dir output
```

Generate documents for one registration while testing:

```bash
python generate_documents.py \
  --config config.json \
  --registration-id 1028 \
  --output-dir output
```

Generated files are written to:

```text
output/invoices/
output/payment_certificates/
```

After changing an override, regenerate that registration with `--registration-id`:

```bash
python generate_documents.py \
  --config config.json \
  --registration-id 1028 \
  --output-dir output
```

This command follows the normal generation rules for both document types and overwrites existing PDFs with the same filenames in the output directory.

## Generation rules

- Rejected and withdrawn registrations are skipped by default.
- An invoice is generated when `Invoice Requested` is a true value (`Yes`, `True`, `1`, `Requested`, or `On`).
- With the supplied configuration, a blank `Invoice Requested` value also generates an invoice. Set `generate_invoice_by_default` to `false` after adding the field if invoices should be opt-in only.
- A payment certificate is generated only when the API reports `is_paid: true`.
- Price, currency, and payment date come directly from Indico.
- The certificate refers to the related invoice and says it is not a request for payment.
- Stripe's PaymentIntent ID is not exposed by this Indico check-in API. The certificate therefore omits a payment reference unless a registration-form field is mapped to `payment_reference`.

## Security and operations

- TLS certificate verification is always enforced.
- Keep the token and generated personal data outside public web directories.
- Commit only fictional settings in `config.example.json`. Keep real recipient details, addresses, VAT numbers, and registration IDs in the ignored local `config.json`.
- Keep generated PDFs out of Git. If you choose a custom `--output-dir`, add that directory to `.gitignore` before generating documents.
- Ignoring or untracking a file does not remove earlier committed versions. Review Git history before making the repository public; previously committed settings and PDFs may remain accessible in old commits.
- Restrict the output directory and service account permissions.
- Use a dedicated personal token for this program so it can be revoked independently.
- Regenerate only the necessary registration with `--registration-id` during routine support work.

## Fonts

The tool automatically looks for DejaVu Sans or Noto Sans in common Linux locations. If billing addresses may contain Japanese characters, explicitly set `font.regular` and `font.bold` to Japanese-capable Unicode TrueType font files, such as IPA Gothic installed on the server.
