FUNCTION scheduleCallEmails
    NAMESPACE cxapp
    PARAMETERS
        scheduledCallDetails AS {"ref": "hrms.ScheduleCallDetails", "version": 1}
    EVENTS
        output
            user AS {"type": "OBJECT", "version": 1, "ref": "hrms.ScheduleCallDetails"}
            contactManger AS {"type": "OBJECT", "version": 1}
    LOGIC
        saveCallDetails1: hrms.saveCallDetails(scheduleCallDetails = Arguments.scheduledCallDetails)
            output
                set1: System.Context.Set(name = "Context.details.user", value = Steps.saveCallDetails1.output.scheduleCallDetails) AFTER Steps.create.output
                    output
                        sendEmail: CoreServices.Email.SendEmail(templateData = Context.details, connectionName = "mail", appCode = "cxapp", address = Context.details.user.emailId, templateName = "meetingScheduledCustomer") AFTER Steps.set1.output, Steps.set.output
                            output
                                generateEvent: System.GenerateEvent(results = {
    "name": "user",
    "value": {
        "isExpression": true,
        "value": "Context.details.user"
    }
}, results = {
    "name": "contactManager",
    "value": {
        "isExpression": true,
        "value": "Context.details.contactManager"
    }
}) AFTER Steps.sendEmail.output, Steps.sendEmail1.output, Steps.sendEmail2.output
                        sendEmail1: CoreServices.Email.SendEmail(templateData = Context.details, appCode = "cxapp", connectionName = "mail", address = Context.details.contactManager.emailId, templateName = "meetingScheduledContactManager") AFTER Steps.set1.output, Steps.set.output
        create: System.Context.Create(name = "details", schema = {
    "type": "OBJECT"
})
        read: CoreServices.Storage.Read(storageName = "Project", appCode = "rim", dataObjectId = Arguments.scheduledCallDetails.projectId)
            output
                set: System.Context.Set(name = "Context.details.contactManager", value = Steps.read.output.result.contactManager) AFTER Steps.create.output
                    output
                        sendEmail2: CoreServices.Email.SendEmail(templateData = Context.details, connectionName = "mail", address = Context.details.user.emailId, appCode = "cxapp", templateName = "bookingConfirmedEmailTemplate") AFTER Steps.set.output, Steps.set1.output