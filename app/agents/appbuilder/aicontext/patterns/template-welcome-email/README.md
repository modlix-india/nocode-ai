# template-welcome-email

Welcome / first-onboarding email.

> **Notes:**
>
> Look at: `templateParts.en.body` containing the full HTML with `${user.emailId}` and `${passwordUsed}` substitutions inline; `toExpression: "${user.emailId}"` routing to the signup user; `subject` set per-language under `templateParts.en.subject`; `templateType: "email"` plus `defaultLanguage: "en"` showing the i18n-keyed parts shape.

**Entity type:** `template`

## Samples

- **marketingai** / `userSignUp` (v48, clientCode=SYSTEM)
  - [marketingai.userSignUp.json](marketingai.userSignUp.json)
