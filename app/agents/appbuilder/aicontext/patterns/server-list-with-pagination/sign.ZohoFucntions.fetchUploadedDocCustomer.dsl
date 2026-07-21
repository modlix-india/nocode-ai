FUNCTION fetchUploadedDocCustomer
    NAMESPACE ZohoFucntions
    PARAMETERS
        projectId AS {"type": "STRING", "version": 1}
        kycId AS {"type": "STRING", "version": 1}
        emailId AS {"type": "STRING", "version": 1, "format": "EMAIL"}
        documentId AS {"type": "STRING", "version": 1}
    EVENTS
        output
            result AS {"type": "OBJECT", "version": 1}
        error
            message AS {"type": "STRING", "version": 1}
    LOGIC
        create: System.Context.Create(name = "filter", schema = {
    "type": "OBJECT"
})
            output
                set: System.Context.Set(name = "Context.filter", value = {
    "operator": "AND",
    "conditions": [
        {
            "field": "recipient_emailId"
        },
        {
            "field": "projectId"
        },
        {
            "field": "kycId"
        },
        {
            "field": "documentId"
        }
    ]
}) AFTER Steps.create.output
                    output
                        set1: System.Context.Set(name = `'Context.filter.conditions[0].value'`, value = Arguments.emailId) AFTER Steps.set.output
                            output
                                set2: System.Context.Set(name = "Context.filter.conditions[1].value", value = Arguments.projectId) AFTER Steps.set1.output
                                    output
                                        set3: System.Context.Set(name = "Context.filter.conditions[2].value", value = Arguments.kycId) AFTER Steps.set2.output
                                            output
                                                set5: System.Context.Set(name = `'Context.filter.conditions[3].value'`, value = Arguments.documentId) AFTER Steps.set3.output
                                                    output
                                                        readPage: CoreServices.Storage.ReadPage(storageName = "uploadedDocuments", filter = Context.filter, appCode = "sign") AFTER Steps.set5.output
                                                            error
                                                                generateEvent1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.readPage.error.result"
    }
})
                                                            output
                                                                generateEvent: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": true,
        "value": "Steps.readPage.output.result.content"
    }
})