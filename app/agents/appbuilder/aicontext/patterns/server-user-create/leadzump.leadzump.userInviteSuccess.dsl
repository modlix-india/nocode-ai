FUNCTION userInviteSuccess
    NAMESPACE leadzump
    PARAMETERS
        userInviteData AS {"type": "OBJECT", "version": 1}
    EVENTS
        output
            mailSent AS {}
    LOGIC
        sendEmail: CoreServices.Email.SendEmail(connectionName = "mail", templateName = "userInviteSuccess", templateData = Arguments.userInviteData)
            output
                generateEvent: System.GenerateEvent(results = {
    "name": "mailSent",
    "value": {
        "isExpression": true,
        "value": "Steps.sendEmail.output.sent"
    }
})