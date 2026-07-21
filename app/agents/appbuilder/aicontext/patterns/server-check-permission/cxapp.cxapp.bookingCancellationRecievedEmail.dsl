FUNCTION bookingCancellationRecievedEmail
    NAMESPACE cxapp
    PARAMETERS
        userDetails AS {"type": "OBJECT", "version": 1}
    EVENTS
        error
            message AS {"type": "STRING", "version": 1}
        result
            response AS {"type": "OBJECT", "version": 1}
    LOGIC
        create1: System.Context.Create(name = "allowToSendEmail", schema = {
    "Type": "BOOLEAN"
})
            output
                create: System.Context.Create(name = "emailTemplateData", schema = {
    "Type": "OBJECT"
}) AFTER Steps.create1.output
                    output
                        readPage: CoreServices.Storage.ReadPage(appCode = "cxapp", storageName = "emailAccessControl") AFTER Steps.create.output
                            output
                                if1: System.If(condition = Steps.readPage.output.result.content.length = 0)
                                    true
                                        set1: System.Context.Set(name = "Context.allowToSendEmail", value = true) AFTER Steps.if1.true
                                    false
                                        set: System.Context.Set(name = "Context.emailTemplateData", value = Steps.readPage.output.result.content[0]) AFTER Steps.if1.false
                                            output
                                                if: System.If(condition = Context.emailTemplateData.isAllEmailsEnabled = true) AFTER Steps.set.output
                                                    true
                                                        set2: System.Context.Set(name = "Context.allowToSendEmail", value = true) AFTER Steps.if.true
                                                    false
                                                        if2: System.If(condition = Context.emailTemplateData.data.cancellationRequestRecievedTemplate.isEmailEnabled = true) AFTER Steps.if.false
                                                            true
                                                                set3: System.Context.Set(name = "Context.allowToSendEmail", value = true) AFTER Steps.if2.true
                                                            false
                                                                set3_Copy_1: System.Context.Set(name = "Context.allowToSendEmail", value = false) AFTER Steps.if2.false
                                    output
                                        if3: System.If(condition = Context.allowToSendEmail = true) AFTER Steps.if1.output
                                            true
                                                sendEmail: CoreServices.Email.SendEmail(templateData = Arguments.userDetails, address = Arguments.userDetails.emailId, connectionName = "mail", appCode = "cxapp", templateName = "cancellationRequestRecievedTemplate") AFTER Steps.if3.true
                                            output
                                                sendEmail1: CoreServices.Email.SendEmail(templateData = Arguments.userDetails, appCode = "cxapp", address = Arguments.userDetails.adminEmail, connectionName = "mail", templateName = "bookingCancelNotifToClient") AFTER Steps.if3.output
                                                    output
                                                        generateEvent: System.GenerateEvent(results = {
    "name": "response",
    "value": {
        "isExpression": true,
        "value": "Steps.sendEmail1.output.sent"
    }
}, eventName = "result")