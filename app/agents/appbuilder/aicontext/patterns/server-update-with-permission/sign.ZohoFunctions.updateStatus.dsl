FUNCTION updateStatus
    NAMESPACE ZohoFunctions
    PARAMETERS
        document AS {"ref": "Sign.uploadStorage.schema", "version": 1}
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
                set4: System.Context.Set(name = "Context.filter", value = {}) AFTER Steps.create.output
                    output
                        set3: System.Context.Set(name = "Context.filter.field", value = `'documentId'`) AFTER Steps.set4.output
                            output
                                set2: System.Context.Set(name = "Context.filter.value", value = Arguments.document.documentId) AFTER Steps.set3.output
                                    output
                                        readPage: CoreServices.Storage.ReadPage(storageName = "uploadedDocuments", appCode = "sign", filter = Context.filter) AFTER Steps.set2.output
                                            error
                                                generateEvent1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.readPage.error.result"
    }
})
                                            output
                                                set: System.Context.Set(name = "Context.fetchedDoc", value = Steps.readPage.output.result.content[0]) AFTER Steps.create1.output
                                                    output
                                                        if: System.If(condition = `Context.fetchedDoc.request_status = "SIGNED"`) AFTER Steps.set.output
                                                            true
                                                                generateEvent: System.GenerateEvent(results = {
    "name": "message",
    "value": "The selected document was already verified"
}) AFTER Steps.if.true
                                                            false
                                                                update: CoreServices.Storage.Update(appCode = "sign", dataObject = Context.updateField, storageName = "uploadedDocuments", dataObjectId = Context.fetchedDoc._id, isPartial = true) AFTER Steps.if.false, Steps.set1.output
                                                                    error
                                                                        generateEvent2: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.update.error.result"
    }
})
                                                                    output
                                                                        generateEvent3: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": true,
        "value": "Steps.update.output.result"
    }
})
        create1: System.Context.Create(name = "fetchedDoc", schema = {
    "type": "OBJECT"
})
        create2: System.Context.Create(name = "updateField", schema = {
    "type": "OBJECT"
})
            output
                set1: System.Context.Set(name = "Context.updateField", value = {
    "request_status": "SIGNED"
}) AFTER Steps.create2.output