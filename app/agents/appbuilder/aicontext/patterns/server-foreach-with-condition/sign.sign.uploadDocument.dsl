FUNCTION uploadDocument
    NAMESPACE sign
    PARAMETERS
        payload AS {"version": 1, "ref": "sign.Payload", "name": "payload", "namespace": "ZohoParams"}
    EVENTS
        error
            message AS {"version": 1, "type": "STRING"}
        output
            dataObject AS {"type": "OBJECT", "version": 1}
    LOGIC
        user: CoreServices.SecurityContext.GetUser()
        getAuthentication: CoreServices.SecurityContext.GetAuthentication()
            output
                if: System.If(condition = Steps.getAuthentication.output.auth.loggedInFromClientId = Steps.getAuthentication.output.auth.user.clientId)
                    true
                        create1: System.Context.Create(name = "dobj", schema = {
    "type": "OBJECT"
}) AFTER Steps.if.true
                            output
                                set1: System.Context.Set(name = "Context.dobj.userId", value = Steps.user.output.user.id) AFTER Steps.create1.output
                                    output
                                        setInternal: System.Context.Set(name = "Context.dobj.isInternal", value = Arguments.payload.isInternal) AFTER Steps.set1.output
                                            output
                                                if1: System.If(condition = Arguments.payload.isInternal) AFTER Steps.setInternal.output
                                                    true
                                                        setLoc: System.Context.Set(name = "Context.dobj.documentLocation", value = Arguments.payload.documentLocation) AFTER Steps.if1.true
                                                            output
                                                                create2: CoreServices.Storage.Create(appCode = "sign", storageName = "UploadedDocuments", dataObject = Context.dobj) AFTER Steps.setLoc.output
                                                                    error
                                                                        generateEvent5: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.create2.error.result"
    }
})
                                                                    output
                                                                        generateEvent4: System.GenerateEvent(results = {
    "name": "dataObject",
    "value": {
        "isExpression": true,
        "value": "Steps.create2.output.result"
    }
})
                                                    false
                                                        pr: CoreServices.REST.PostRequest(headers = {
    "Content-Type": "multipart/form-data"
}, payload = Arguments.payload, appCode = "sign", connectionName = "ZohoRestSign", url = "/api/v1/requests") AFTER Steps.if1.false
                                                            error
                                                                generateEvent1: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.pr.error.data"
    }
})
                                                            output
                                                                set6: System.Context.Set(name = "Context.dobj.responseObj", value = Steps.pr.output.data)
                                                                    output
                                                                        create: CoreServices.Storage.Create(appCode = "sign", storageName = "UploadedDocuments", dataObject = Context.dobj) AFTER Steps.set6.output
                                                                            error
                                                                                generateEvent2: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.create.error.result"
    }
})
                                                                            output
                                                                                generateEvent: System.GenerateEvent(results = {
    "name": "dataObject",
    "value": {
        "isExpression": true,
        "value": "Steps.create.output.result"
    }
})
                    false
                        generateEvent3: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "You don't have access to upload"
    }
}) AFTER Steps.if.false