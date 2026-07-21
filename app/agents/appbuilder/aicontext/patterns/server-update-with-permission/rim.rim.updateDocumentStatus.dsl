FUNCTION updateDocumentStatus
    NAMESPACE rim
    PARAMETERS
        bookingId AS {"type": "STRING", "version": 1}
        documentId AS {"type": "STRING", "version": 1}
        isEoi AS {"type": "BOOLEAN", "version": 1, "defaultValue": false}
        othersId AS {"type": "INTEGER", "version": 1, "defaultValue": -1}
        delete AS {"type": "BOOLEAN", "version": 1, "defaultValue": false}
        isInternal AS {"type": "BOOLEAN", "version": 1, "defaultValue": false}
    EVENTS
        output
            document AS {"type": "OBJECT", "version": 1}
        error
            message AS {"type": "STRING", "version": 1}
    LOGIC
        create: System.Context.Create(schema = {
    "ref": "rim.Bookings.BookingDetails"
}, name = "bookingObj")
            output
                read: CoreServices.Storage.Read(storageName = "Bookings", dataObjectId = Arguments.bookingId, appCode = "rim") AFTER Steps.create.output
                    error
                        generateEvent: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.read.error.result"
    }
})
                    output
                        ifForEoi: System.If(condition = Arguments.isEoi) AFTER Steps.read.output
                            true
                                set: System.Context.Set(name = "Context.bookingObj", value = Steps.read.output.result) AFTER Steps.ifForEoi.true
                                    output
                                        ifEoiTrueIsInternal: System.If(condition = Arguments.isInternal) AFTER Steps.set.output
                                            true
                                                ifForEoiInternalUpdate: System.If(condition = Context.bookingObj.documents.eoiInternal.documentId = Arguments.documentId) AFTER Steps.ifEoiTrueIsInternal.true
                                                    true
                                                        ifForEoiInternalDelete: System.If(condition = Arguments.delete) AFTER Steps.ifForEoiInternalUpdate.true
                                                            true
                                                                objectDeleteKeyInternalEOI: System.Object.ObjectDeleteKey(source = Context.bookingObj.documents, key = "eoiInternal") AFTER Steps.ifForEoiInternalDelete.true
                                                                    output
                                                                        set6: System.Context.Set(name = "Context.bookingObj.documents", value = Steps.objectDeleteKeyInternalEOI.output.value)
                                                            output
                                                                updateForEoiInternal: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.bookingObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.ifForEoiInternalDelete.output
                                                                    error
                                                                        generateEvent9: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForEoiInternal.error.result"
    }
})
                                                                    output
                                                                        generateEvent10: System.GenerateEvent(results = {
    "name": "document",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForEoiInternal.output.result"
    }
})
                                                    false
                                                        generateEvent8: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Please provide valid document"
    }
}) AFTER Steps.ifForEoiInternalUpdate.false
                                            false
                                                ifEoiUpdate: System.If(condition = Context.bookingObj.documents.eoi.documentId = Arguments.documentId) AFTER Steps.ifEoiTrueIsInternal.false
                                                    true
                                                        ifEoiDelete: System.If(condition = Arguments.delete) AFTER Steps.ifEoiUpdate.true
                                                            true
                                                                objectDeleteKey: System.Object.ObjectDeleteKey(source = Context.bookingObj.documents, key = "eoi") AFTER Steps.ifEoiDelete.true
                                                                    output
                                                                        set5: System.Context.Set(name = "Context.bookingObj.documents", value = Steps.objectDeleteKey.output.value)
                                                            output
                                                                updateForEoi: CoreServices.Storage.Update(dataObjectId = Arguments.bookingId, appCode = "rim", dataObject = Context.bookingObj, storageName = "Bookings") AFTER Steps.ifEoiDelete.output
                                                                    error
                                                                        generateEvent6: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForEoi.error.result"
    }
})
                                                                    output
                                                                        generateEvent7: System.GenerateEvent(results = {
    "name": "document",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForEoi.output.result"
    }
})
                                                    false
                                                        generateEvent3: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Please provide valid document"
    }
}, eventName = "error") AFTER Steps.ifEoiUpdate.false
                            false
                                ifForOthers: System.If(condition = Arguments.othersId = -1) AFTER Steps.ifForEoi.false
                                    true
                                        generateEvent1: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Please provide others document location id"
    }
}) AFTER Steps.ifForOthers.true
                                    false
                                        set1: System.Context.Set(name = "Context.bookingObj", value = Steps.read.output.result) AFTER Steps.ifForOthers.false
                                            output
                                                ifEoiFalseIsInternal: System.If(condition = Arguments.isInternal) AFTER Steps.set1.output
                                                    false
                                                        ifOthersUpdate: System.If(condition = `'Context.bookingObj.documents.others[{{Arguments.othersId}}].documentId = Arguments.documentId'`) AFTER Steps.ifEoiFalseIsInternal.false
                                                            true
                                                                ifOthersDelete: System.If(condition = Arguments.delete) AFTER Steps.ifOthersUpdate.true
                                                                    true
                                                                        delete: System.Array.Delete(source = Context.bookingObj.documents.others, element = Context.bookingObj.documents.others[{{Arguments.othersId}}]) AFTER Steps.ifOthersDelete.true
                                                                            output
                                                                                set4: System.Context.Set(name = "Context.bookingObj.documents.others", value = Steps.delete.output.result)
                                                                    output
                                                                        update: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.bookingObj, dataObjectId = Arguments.bookingId, storageName = "Bookings") AFTER Steps.ifOthersDelete.output
                                                                            error
                                                                                generateEvent5: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.update.error.result"
    }
})
                                                                            output
                                                                                generateEvent4: System.GenerateEvent(results = {
    "name": "document",
    "value": {
        "isExpression": true,
        "value": "Steps.update.output.result"
    }
})
                                                            false
                                                                generateEvent2: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Please provide valid document"
    }
}, eventName = "error") AFTER Steps.ifOthersUpdate.false