FUNCTION signUpOTPEmail
    NAMESPACE cxapp
    PARAMETERS
        email AS {"type": "STRING", "version": 1}
        otp AS {"version": 1, "type": "STRING"}
    EVENTS
        error
            message AS {"type": "STRING", "version": 1}
        output
            result AS {"type": "OBJECT", "version": 1}
    LOGIC
        create: System.Context.Create(name = "user", schema = {
    "type": "OBJECT"
})
            output
                set: System.Context.Set(name = "Context.user.email", value = Arguments.email) AFTER Steps.create.output
                    output
                        set1: System.Context.Set(name = "Context.user.otp", value = Arguments.otp) AFTER Steps.set.output
                            output
                                sendEmail: CoreServices.Email.SendEmail(templateData = Context.user, connectionName = "mail", appCode = "cxapp", address = Context.user.emailId, templateName = "otpTemplate") AFTER Steps.set1.output
                                    output
                                        generateEvent: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": true,
        "value": "Context.user"
    }
}) AFTER Steps.sendEmail.output