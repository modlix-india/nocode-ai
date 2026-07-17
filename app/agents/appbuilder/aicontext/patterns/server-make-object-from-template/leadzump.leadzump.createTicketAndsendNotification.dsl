FUNCTION createTicketAndsendNotification
    NAMESPACE leadzump
    PARAMETERS
        clientCode AS {"type": "STRING", "version": 1}
        appCode AS {"type": "STRING", "version": 1}
        leadDetails AS {"type": "OBJECT", "version": 1}
        campaignDetails AS {"type": "OBJECT", "version": 1}
    EVENTS
        output
            result AS {"type": "STRING", "version": 1}
    LOGIC
        create: System.Context.Create(name = "leadPayload", schema = {
    "type": "OBJECT"
})
            output
                comment: System.Context.Set(name = "Context.leadPayload.comment", value = Arguments.comment) AFTER Steps.create.output
                    output
                        appCode: System.Context.Set(name = "Context.leadPayload.appCode", value = Arguments.appCode) AFTER Steps.comment.output
                            output
                                clientCode: System.Context.Set(name = "Context.leadPayload.clientCode", value = Arguments.clientCode) AFTER Steps.appCode.output
                                    output
                                        leadDetails: System.Context.Set(name = "Context.leadPayload.leadDetails", value = Arguments.leadDetails) AFTER Steps.clientCode.output
                                            output
                                                campDetails: System.Context.Set(name = "Context.leadPayload.campaignDetails", value = Arguments.campaignDetails) AFTER Steps.leadDetails.output
                                                    output
                                                        make: System.Make(resultShape = {
    "clientCode": "{{Context.leadPayload.clientCode}}",
    "appCode": "{{Context.leadPayload.appCode}}",
    "leadDetails": {
        "email": {
            "address": "{{Context.leadPayload.leadDetails.email}}"
        },
        "fullName": "{{Context.leadPayload.leadDetails.fullName}}",
        "phone": {
            "number": "{{Context.leadPayload.leadDetails.phone}}"
        },
        "workEmail": {
            "address": "{{Context.leadPayload.leadDetails.workEmail}}"
        },
        "workPhoneNumber": {
            "number": "{{Context.leadPayload.leadDetails.workPhoneNumber}}"
        },
        "whatsappNumber": {
            "number": "{{Context.leadPayload.leadDetails.whatsappNumber}}"
        },
        "platform": "{{Context.leadPayload.leadDetails.platform}}",
        "subSource": "{{Context.leadPayload.leadDetails.subSource}}",
        "source": "{{Context.leadPayload.leadDetails.source}}",
        "customFields": "{{Context.leadPayload.leadDetails.customFields}}"
    },
    "campaignDetails": "{{Context.leadPayload.campaignDetails}}"
}) AFTER Steps.campDetails.output
                                                            output
                                                                createForCampaign: EntityProcessor.Ticket.CreateForCampaign(campaignTicketRequest = Steps.make.output.value)
                                                                    output
                                                                        if: System.If(condition = Steps.createForCampaign.output.created)
                                                                            false
                                                                                sendDealCreatedNotification: CoreServices.Notification.SendNotification(notificationName = "dealCreated", connectionName = "notification", appCode = "leadzump", targetId = Steps.createForCampaign.output.created.assignedUserId, clientCode = Steps.createForCampaign.output.created.clientCode, notificationCategory = `"DEAL_CREATE"`) AFTER Steps.if.false
                                                                                    output
                                                                                        if1: System.If(condition = Steps.sendDealCreatedNotification.output.sent)
                                                                                            false
                                                                                                generateEvent: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": false,
        "value": "Deal created and notification sent successfully"
    }
}) AFTER Steps.if1.false