FUNCTION rejectingBookingCancellation
    NAMESPACE cxapp
    PARAMETERS
        details AS {"type": "OBJECT", "version": 1}
    EVENTS
        error
            message AS {"type": "STRING", "version": 1}
        output
            sent AS {"type": "OBJECT", "version": 1}
            result AS {"type": "OBJECT", "version": 1}
    LOGIC
        sendEmail: CoreServices.Email.SendEmail(templateData = Arguments.details, address = ``, connectionName = "mail", templateName = "cancellationRejectionEmail", appCode = "cxapp")
            output
                generateEvent: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": true,
        "value": "Steps.sendEmail.output.sent"
    }
})