# template-password-reset

Password reset email with token link.

> **Notes:**
> 
> Look at: `templateParts.en.body` embedding the reset link as `${urlPrefix}/setPassword/${token}` (the two required placeholders the platform substitutes); `toExpression` set to `${user.emailId}` to address the recipient; `templateType: "email"` with `defaultLanguage: "en"` and a paired `subject` ("Reset your password") inside `templateParts.en`.

**Entity type:** `template`

## Samples

- **marketingai** / `resetPasswordTemplate` (v48, clientCode=SYSTEM)
  - [marketingai.resetPasswordTemplate.json](marketingai.resetPasswordTemplate.json)
