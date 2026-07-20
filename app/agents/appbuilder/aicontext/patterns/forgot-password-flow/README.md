# forgot-password-flow

Request-reset → verify OTP → set new password sequence.

**Entity type:** `page`

**Notes:**

Often spans 2-3 pages with a Stepper or modal sequence.

## Samples

- **landingpages** / `resetPasswordNew` (v2, clientCode=SYSTEM)
  - [landingpages.resetPasswordNew.json](landingpages.resetPasswordNew.json)
  - [landingpages.resetPasswordNew.tree.txt](landingpages.resetPasswordNew.tree.txt)
  - [landingpages.resetPasswordNew.event.validations.dsl](landingpages.resetPasswordNew.event.validations.dsl)
  - [landingpages.resetPasswordNew.event.eightCharactersMin.dsl](landingpages.resetPasswordNew.event.eightCharactersMin.dsl)
  - [landingpages.resetPasswordNew.event.oneUpperCaseCheck.dsl](landingpages.resetPasswordNew.event.oneUpperCaseCheck.dsl)
- **modlix** / `forgotPassword` (v8, clientCode=SYSTEM)
  - [modlix.forgotPassword.json](modlix.forgotPassword.json)
  - [modlix.forgotPassword.tree.txt](modlix.forgotPassword.tree.txt)
  - [modlix.forgotPassword.event.onLoad.dsl](modlix.forgotPassword.event.onLoad.dsl)
  - [modlix.forgotPassword.event.NewTimer.dsl](modlix.forgotPassword.event.NewTimer.dsl)
- **landingpages** / `forgotPasswordReset` (v23, clientCode=SYSTEM)
  - [landingpages.forgotPasswordReset.json](landingpages.forgotPasswordReset.json)
  - [landingpages.forgotPasswordReset.tree.txt](landingpages.forgotPasswordReset.tree.txt)
  - [landingpages.forgotPasswordReset.event.validations.dsl](landingpages.forgotPasswordReset.event.validations.dsl)
  - [landingpages.forgotPasswordReset.event.eightCharactersMin.dsl](landingpages.forgotPasswordReset.event.eightCharactersMin.dsl)
  - [landingpages.forgotPasswordReset.event.oneUpperCaseCheck.dsl](landingpages.forgotPasswordReset.event.oneUpperCaseCheck.dsl)
