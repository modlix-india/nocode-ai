FUNCTION sendAadharOTP
    NAMESPACE cxapp
    PARAMETERS
        aadharNumber AS {"type": "STRING", "version": 1}
    EVENTS
        output
            response AS {"type": "OBJECT", "version": 1}
        error
            response AS {"type": "OBJECT", "version": 1}
    LOGIC
        create: System.Context.Create(name = "payload", schema = {
    "type": "OBJECT"
})
            output
                make: System.Make(resultShape = {
    "@entity": "in.co.sandbox.kyc.aadhaar.okyc.otp.request",
    "aadhaar_number": "{{Arguments.aadharNumber}}",
    "consent": "Y",
    "reason": "User KYC"
}) AFTER Steps.create.output
                    output
                        set: System.Context.Set(name = "Context.payload", value = Steps.make.output.value)
                            output
                                postRequest: CoreServices.REST.PostRequest(connectionName = "KYCVerification", url = "/kyc/aadhaar/okyc/otp", headers = {
    "Authorization": "eyJ0eXAiOiJKV1MiLCJhbGciOiJSU0FTU0FfUFNTX1NIQV81MTIiLCJraWQiOiIwYzYwMGUzMS01MDAwLTRkYTItYjM3YS01ODdkYTA0ZTk4NTEifQ.eyJ3b3Jrc3BhY2VfaWQiOiJmOTIxMTdjYi04MTdmLTQ4ZTMtYmNjNi04NWIyN2UzNjk1YzkiLCJzdWIiOiJrZXlfbGl2ZV8xNGU4NzdhNDFmNGU0MWI0OWY1MzcyNDZhOThkODdhYyIsImFwaV9rZXkiOiJrZXlfbGl2ZV8xNGU4NzdhNDFmNGU0MWI0OWY1MzcyNDZhOThkODdhYyIsImF1ZCI6IkFQSSIsImludGVudCI6IkFDQ0VTU19UT0tFTiIsImlzcyI6InByb2QxLWFwaS5zYW5kYm94LmNvLmluIiwiaWF0IjoxNzc0MzM3MDQyLCJleHAiOjE3NzQ0MjM0NDJ9.vIhB-z3ECXn6JRdwAvi9UJd0jbTwZRxX0n-R74pQvigC8FUyoP4rvwyIW8OnqTSOGVhlx-nFQVTUjrUqVR10vPfkOruLZ-Q1CV72Pmky28svuxle33x6pGrgtjeeLsQowLN3oK-UwSemI7HGZDr74JWvZPKR5Btvx_jqTq_NBweF3bSmk8az2XpAPl4014EJfN_shkzxHL5yFpub0yaY_2kLqS8Opuz_0ePghoTTihtz8Fe6j5XyEKiUM0fn8ZKRBKHHuybi3Mw3I1UZoM8FhsThwCuaFe6LPNSGe8VIowVagJ83UFacsEbAhphEqx7_vaPpJirW4uc77yxK_dGZwg",
    "x-api-key": "key_live_14e877a41f4e41b49f537246a98d87ac",
    "Content-Type": "application/json"
}, payload = Context.payload, fileType = "static") AFTER Steps.set.output
                                    error
                                        generateEvent_Copy_1: System.GenerateEvent(results = {
    "name": "response",
    "value": {
        "isExpression": true,
        "value": "Steps.postRequest.error.data"
    }
}, eventName = "error")
                                    output
                                        generateEvent: System.GenerateEvent(results = {
    "name": "response",
    "value": {
        "isExpression": true,
        "value": "Steps.postRequest.output.data"
    }
})