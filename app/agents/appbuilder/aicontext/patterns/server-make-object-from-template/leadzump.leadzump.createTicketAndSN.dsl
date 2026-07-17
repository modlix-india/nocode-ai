FUNCTION createTicketAndSN
    NAMESPACE leadzump
    PARAMETERS
        ticketRequest AS {"type": "OBJECT", "version": 1}
        loggedInUser AS {"type": "LONG", "version": 1}
    EVENTS
        output
            result AS {}
            notificationStatus AS {"type": "STRING", "version": 1}
        error
            result AS {}
    LOGIC
        create: System.Context.Create(name = "leadObj", schema = {
    "type": "OBJECT"
})
            output
                appCode: System.Context.Set(name = "Context.leadObj", value = Arguments.ticketRequest) AFTER Steps.create.output
                    output
                        make: System.Make(resultShape = {
    "name": "{{Context.leadObj.name}}",
    "phoneNumber": {
        "number": "{{Context.leadObj.phoneNumber}}"
    },
    "email": {
        "address": "{{Context.leadObj.email}}"
    },
    "source": "{{Context.leadObj.source}}",
    "subSource": "{{Context.leadObj.subSource}}",
    "productId": {
        "id": "{{Context.leadObj.productId}}"
    }
}) AFTER Steps.appCode.output
                            output
                                createRequest: EntityProcessor.Ticket.CreateRequest(ticketRequest = Steps.make.output.value)
                                    error
                                        if2: System.If(condition = Steps.createRequest.error.error)
                                            true
                                                generateEvent_Copy_1_Copy_1: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": true,
        "value": "Steps.createRequest.error.error"
    }
}, results = ``, eventName = "error") AFTER Steps.if2.true
                                    output
                                        if: System.If(condition = Steps.createRequest.output.created)
                                            true
                                                create_Copy_1: System.Context.Create(name = "notificationPayload", schema = {
    "type": "OBJECT"
}) AFTER Steps.if.true
                                                    output
                                                        set: System.Context.Set(name = "Context.notificationPayload.source", value = Steps.createRequest.output.created.source) AFTER Steps.create_Copy_1.output
                                                            output
                                                                set_Copy_1: System.Context.Set(name = "Context.notificationPayload.subSource", value = Steps.createRequest.output.created.subSource) AFTER Steps.set.output
                                                                    output
                                                                        makeId: System.Make(resultShape = {
    "id": "{{Steps.createForCampaign.output.created.productId}}"
}) AFTER Steps.set_Copy_1.output
                                                                            output
                                                                                getWalkInProduct: EntityProcessor.ProductWalkInForm.GetWalkInProduct(appCode = "leadzump", clientCode = Steps.createRequest.output.created.clientCode, productId = Steps.makeId.output.value)
                                                                                    output
                                                                                        set_Copy_1_Copy_1: System.Context.Set(name = "Context.notificationPayload.productName", value = Steps.getWalkInProduct.output.result.name)
                                                                                            output
                                                                                                set_Copy_1_Copy_2: System.Context.Set(name = "Context.notificationPayload.code", value = Steps.createRequest.output.created.code) AFTER Steps.set_Copy_1_Copy_1.output
                                                                                                    output
                                                                                                        sendDealCreatedNotification: CoreServices.Notification.SendNotification(notificationName = "dealCreated", connectionName = "notification", appCode = "leadzump", targetId = Steps.createRequest.output.created.assignedUserId, notificationCategory = `"DEAL_CREATE"`, payload = Context.notificationPayload) AFTER Steps.set_Copy_1_Copy_2.output
                                                                                                            output
                                                                                                                if1_Copy_1: System.If(condition = Steps.sendDealCreatedNotification.output.sent)
                                                                                                                    true
                                                                                                                        generateEvent: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": true,
        "value": "Steps.createRequest.output.created"
    }
}, results = {
    "name": "notificationStatus",
    "value": {
        "isExpression": false,
        "value": "Notification sent!"
    }
}) AFTER Steps.if1_Copy_1.true
                                                                                                                    false
                                                                                                                        generateEvent_Copy_2: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": false,
        "value": "Not error"
    }
}, eventName = "error") AFTER Steps.if1_Copy_1.false