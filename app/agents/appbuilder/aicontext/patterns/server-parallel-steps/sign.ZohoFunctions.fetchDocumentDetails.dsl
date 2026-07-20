FUNCTION fetchDocumentDetails
    NAMESPACE ZohoFunctions
    PARAMETERS
        emailId AS {"type": "STRING", "version": 1}
    EVENTS
        error
            message AS {"type": "STRING", "version": 1}
        output
            result AS {"type": "ARRAY", "version": 1}
    LOGIC
        create: System.Context.Create(name = "filter", schema = {
    "type": "OBJECT"
})
            output
                set: System.Context.Set(name = "Context.filter", value = {
    "field": "recipient_emailId"
}) AFTER Steps.create.output
                    output
                        set1: System.Context.Set(name = "Context.filter.value", value = Arguments.emailId) AFTER Steps.set.output
                            output
                                readPage: CoreServices.Storage.ReadPage(storageName = "uploadedDocuments", appCode = "sign", filter = Context.filter) AFTER Steps.set1.output
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