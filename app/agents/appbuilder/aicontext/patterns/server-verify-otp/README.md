# server-verify-otp

Validate an entered OTP.

**Notes:**

Look at: PARAMETERS taking `reference` + `otp` strings; `System.Context.Create` + `Set` staging a payload object; `CoreServices.REST.PostRequest` to an external KYC verify endpoint with auth headers; `System.GenerateEvent` on both `error` and `output` branches forwarding `Steps.postRequest.*.data`.

**Entity type:** `server_function`

## Samples

- **cxapp** / `cxapp.OTPVerfication` (v2, clientCode=SYSTEM)
  - [cxapp.cxapp.OTPVerfication.json](cxapp.cxapp.OTPVerfication.json)
  - [cxapp.cxapp.OTPVerfication.dsl](cxapp.cxapp.OTPVerfication.dsl)
