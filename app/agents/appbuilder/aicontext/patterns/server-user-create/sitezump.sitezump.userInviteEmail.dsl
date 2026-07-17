FUNCTION userInviteEmail
    NAMESPACE sitezump
    PARAMETERS
        user AS {"type": "OBJECT", "version": 1}
    EVENTS
        output
            sent AS {}
    LOGIC
        sendEmail: CoreServices.Email.SendEmail(templateData = Arguments.user, connectionName = "mail", templateName = "userInvite")
            output
                generateEvent: System.GenerateEvent(results = {
    "name": "sent",
    "value": {
        "isExpression": true,
        "value": "Steps.sendEmail.output.sent"
    }
})