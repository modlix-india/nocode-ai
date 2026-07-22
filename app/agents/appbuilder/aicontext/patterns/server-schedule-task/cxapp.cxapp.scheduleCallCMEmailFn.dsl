FUNCTION scheduleCallCMEmailFn
    NAMESPACE cxapp
    PARAMETERS
        scheduledCallDetails AS {"ref": "hrms.ScheduleCallDetails", "version": 1}
    EVENTS
        output
            contactManger AS {"type": "OBJECT", "version": 1}
            user AS {"type": "OBJECT", "version": 1, "ref": "hrms.ScheduleCallDetails"}
    LOGIC
        create: System.Context.Create(name = "details", schema = {
    "type": "OBJECT"
})
        saveCallDetails: hrms.saveCallDetails(scheduleCallDetails = Arguments.scheduledCallDetails)
            output
                settinguserdetails: System.Context.Set(name = "Context.details.user", value = Steps.saveCallDetails.output.scheduleCallDetails) AFTER Steps.create.output
        read: CoreServices.Storage.Read(dataObjectId = Arguments.scheduledCallDetails.projectId, storageName = "Project", appCode = "rim")
            output
                settingcontactmangerdetails: System.Context.Set(name = "Context.details.contactManager", value = Steps.read.output.result.contactManager) AFTER Steps.create.output
                    output
                        readPage: CoreServices.Storage.ReadPage(storageName = "BusinessDetails", appCode = "cxapp") AFTER Steps.settingcontactmangerdetails.output, Steps.settinguserdetails.output
                            output
                                getAppUrl: CoreServices.Security.GetAppUrl(appCode = "cxapp") AFTER Steps.readPage.output
                                    output
                                        set: System.Context.Set(name = "Context.details.imageDomain", value = `'{{Steps.getAppUrl.output.result}}/{{Steps.readPage.output.result.content[0].image}}'`)
                                            output
                                                set1: System.Context.Set(name = "Context.details.fromEmailId", value = `'{{Steps.readPage.output.result.content[0].defaultEmailId}}'`) AFTER Steps.set.output
                                                    output
                                                        sendEmailtocontactmanger: CoreServices.Email.SendEmail(templateData = Context.details, address = ``, templateName = "meetingScheduledContactManager", appCode = "cxapp", connectionName = "mail") AFTER Steps.set1.output
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
}) AFTER Steps.sendEmailtocontactmanger.output
                                                        create1: System.Context.Create(name = "allowToSendEmail", schema = {
    "Type": "BOOLEAN"
}) AFTER Steps.set1.output
                                                            output
                                                                create_Copy_1: System.Context.Create(name = "emailTemplateData", schema = {
    "Type": "OBJECT"
}) AFTER Steps.create1.output
                                                                    output
                                                                        readEmailtemplateconf: CoreServices.Storage.ReadPage(appCode = "cxapp", storageName = "emailAccessControl") AFTER Steps.create_Copy_1.output
                                                                            output
                                                                                if1: System.If(condition = Steps.readEmailtemplateconf.output.result.content.length = 0)
                                                                                    true
                                                                                        set1_Copy_1: System.Context.Set(name = "Context.allowToSendEmail", value = true) AFTER Steps.if1.true
                                                                                    false
                                                                                        set_Copy_1: System.Context.Set(name = "Context.emailTemplateData", value = Steps.readEmailtemplateconf.output.result.content[0]) AFTER Steps.if1.false
                                                                                            output
                                                                                                if: System.If(condition = Context.emailTemplateData.isAllEmailsEnabled = true) AFTER Steps.set_Copy_1.output
                                                                                                    true
                                                                                                        set2: System.Context.Set(name = "Context.allowToSendEmail", value = true) AFTER Steps.if.true
                                                                                                    false
                                                                                                        if2: System.If(condition = Context.emailTemplateData.data.meetingScheduledCustomer.isEmailEnabled = true) AFTER Steps.if.false
                                                                                                            true
                                                                                                                set3: System.Context.Set(name = "Context.allowToSendEmail", value = true) AFTER Steps.if2.true
                                                                                                            false
                                                                                                                set3_Copy_1: System.Context.Set(name = "Context.allowToSendEmail", value = false) AFTER Steps.if2.false
                                                                                    output
                                                                                        if3: System.If(condition = Context.allowToSendEmail = true) AFTER Steps.if1.output
                                                                                            true
                                                                                                sendEmailtocustomer: CoreServices.Email.SendEmail(templateData = Context.details, address = ``, connectionName = "mail", appCode = "cxapp", templateName = "meetingScheduledCustomer") AFTER Steps.if3.true