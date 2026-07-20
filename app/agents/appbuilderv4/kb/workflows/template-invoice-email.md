# template-invoice-email

Invoice attached / linked email.

> **Notes:**
> 
> Look at: `templateParts.en.body` holding the full inline-styled HTML receipt with `${receiptNo}`, `${bookingName}`, `${totalAmountPaid}`, `${transactionDetails.paymentMethod}` placeholders (dot-path access into nested objects); `templateType: "email"` with `subject` and `fromExpression: "<EMAIL>"` driving delivery, and `defaultLanguage: "en"` keying which `templateParts.<lang>` block is used.

**Entity type:** `template`

## Samples

- **cxapp** / `receiptTemplate` (v73, clientCode=SYSTEM)
  - [cxapp.receiptTemplate.json](cxapp.receiptTemplate.json)
- **cxapp** / `receiptsTemplate` (v73, clientCode=SYSTEM)
  - [cxapp.receiptsTemplate.json](cxapp.receiptsTemplate.json)
