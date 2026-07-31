FUNCTION deleteAllDocumentsInBooking
    NAMESPACE rim
    PARAMETERS
        bookingId AS {"type": "STRING", "version": 1}
        docTypeList AS ARRAY OF STRING
    EVENTS
        result
            response AS {"type": "OBJECT", "version": 1}
        error
            message AS {"type": "STRING", "version": 1}
    LOGIC
        fitler: System.Context.Create(name = "filter", schema = {
    "type": "OBJECT"
})
            output
                field: System.Context.Set(name = "Context.filter.field", value = "bookingId") AFTER Steps.fitler.output
                    output
                        value: System.Context.Set(name = "Context.filter.value", value = Arguments.bookingId) AFTER Steps.field.output
                            output
                                deleteByFilter: CoreServices.Storage.DeleteByFilter(filter = Context.filter, storageName = "Documents", appCode = "rim") AFTER Steps.value.output
                                    error
                                        generateEvent2: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.deleteByFilter.error.result"
    }
})
                                    output
                                        create: System.Context.Create(schema = {
    "type": "OBJECT"
}, name = "bookingObj") AFTER Steps.deleteByFilter.output
                                            output
                                                if: System.If(condition = Arguments.docTypeList) AFTER Steps.create.output
                                                    true
                                                        set: System.Context.Set(name = "Context.bookingObj", value = {}) AFTER Steps.if.true
                                                            output
                                                                read: CoreServices.Storage.Read(appCode = "rim", storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.set.output
                                                                    output
                                                                        bookingObj: System.Context.Set(name = "Context.bookingObj", value = Steps.read.output.result)
                                                                            output
                                                                                forEachLoop: System.Loop.ForEachLoop(source = Arguments.docTypeList) AFTER Steps.bookingObj.output
                                                                                    iteration
                                                                                        ifEoi: System.If(condition = `Steps.forEachLoop.iteration.each="eoi"`)
                                                                                            true
                                                                                                objectDeleteKey: System.Object.ObjectDeleteKey(source = Context.bookingObj.documents, key = "eoi") AFTER Steps.ifEoi.true
                                                                                                    output
                                                                                                        set1: System.Context.Set(name = "Context.bookingObj.documents", value = Steps.objectDeleteKey.output.value)
                                                                                            false
                                                                                                ifAgreements: System.If(condition = `Steps.forEachLoop.iteration.each = "agreements"`) AFTER Steps.ifEoi.false
                                                                                                    true
                                                                                                        objectDeleteKey1: System.Object.ObjectDeleteKey(source = Context.bookingObj.documents, key = "agreements") AFTER Steps.ifAgreements.true
                                                                                                            output
                                                                                                                set2: System.Context.Set(name = "Context.bookingObj.documents", value = Steps.objectDeleteKey1.output.value)
                                                                                                    false
                                                                                                        ifLegal: System.If(condition = `Steps.forEachLoop.iteration.each = "legal"`) AFTER Steps.ifAgreements.false
                                                                                                            true
                                                                                                                objectDeleteKey2: System.Object.ObjectDeleteKey(source = Context.bookingObj.documents, key = "legal") AFTER Steps.ifLegal.true
                                                                                                                    output
                                                                                                                        set3: System.Context.Set(name = "Context.bookingObj.documents", value = Steps.objectDeleteKey2.output.value)
                                                                                    output
                                                                                        update: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.bookingObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.forEachLoop.output
                                                                                            error
                                                                                                generateEvent1: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.update.error.result"
    }
})
                                                                                            output
                                                                                                generateEvent: System.GenerateEvent(results = {
    "name": "response",
    "value": {
        "isExpression": true,
        "value": "Steps.update.output.result"
    }
}, eventName = "result")