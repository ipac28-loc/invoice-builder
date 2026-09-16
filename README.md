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

Edit `config.json`:

```json
"indico": {
  "base_url": "https://indico.pasj.jp",
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
- Restrict the output directory and service account permissions.
- Use a dedicated personal token for this program so it can be revoked independently.
- Regenerate only the necessary registration with `--registration-id` during routine support work.

## Fonts

The tool automatically looks for DejaVu Sans or Noto Sans in common Linux locations. If billing addresses may contain Japanese characters, explicitly set `font.regular` and `font.bold` to Japanese-capable Unicode TrueType font files, such as IPA Gothic installed on the server.
