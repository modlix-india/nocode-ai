FUNCTION updateDocumentsInternal
    NAMESPACE rim
    PARAMETERS
        bookingId AS {"type": "STRING", "version": 1, "minLength": 3}
        documentObject AS {"type": "OBJECT", "version": 1}
        isEoi AS {"type": "BOOLEAN", "version": 1, "defaultValue": false}
    EVENTS
        output
            data AS {"type": "OBJECT", "version": 1}
        error
            message AS {"type": "STRING", "version": 1}
    LOGIC
        createCon: System.Context.Create(schema = {
    "type": "OBJECT"
}, name = "dataObj")
            output
                ifInternal: System.If(condition = Arguments.documentObject.isInternal) AFTER Steps.createCon.output
                    true
                        setIntDocLocation: System.Context.Set(name = "Context.dataObj.documentLocation", value = Arguments.documentObject.documentLocation) AFTER Steps.ifInternal.true
                            output
                                lastIndexOf: System.String.LastIndexOf(string = Arguments.documentObject.documentLocation, searchString = "/") AFTER Steps.setIntDocLocation.output
                                    output
                                        substring: System.String.SubString(index = {{ Steps.lastIndexOf.output.result + 1}}, string = Arguments.documentObject.documentLocation, secondIndex = Arguments.documentObject.documentLocation.length)
                                            output
                                                setIntDocName: System.Context.Set(name = "Context.dataObj.documentName", value = Steps.substring.output.result)
                    false
                        setDocName: System.Context.Set(value = Arguments.documentObject.responseObj.requests.document_ids[0].document_name, name = "Context.dataObj.documentName") AFTER Steps.ifInternal.false
                    output
                        setId: System.Context.Set(value = Arguments.documentObject._id, name = "Context.dataObj.documentId") AFTER Steps.ifInternal.output
                            output
                                set2: System.Context.Set(name = `'Context.dataObj.isInternal'`, value = Arguments.documentObject.isInternal) AFTER Steps.setId.output
                                    output
                                        setFileType: System.Context.Set(name = "Context.dataObj.fileType", value = Arguments.documentObject.fileType) AFTER Steps.set2.output
                                            output
                                                setStatus: System.Context.Set(value = "NOT_SIGNED", name = "Context.dataObj.requestStatus") AFTER Steps.setFileType.output
                                                    output
                                                        setDocStatus: System.Context.Set(name = "Context.dataObj.documentStatus", value = Arguments.documentObject.documentStatus) AFTER Steps.setStatus.output
                                                            output
                                                                read: CoreServices.Storage.Read(storageName = "Bookings", appCode = "rim", dataObjectId = Arguments.bookingId) AFTER Steps.setDocStatus.output
                                                                    error
                                                                        generateEvent: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.read.error.result"
    }
})
                                                                    output
                                                                        create: System.Context.Create(schema = {
    "type": "OBJECT"
}, name = "updateObj") AFTER Steps.read.output
                                                                            output
                                                                                set: System.Context.Set(name = "Context.updateObj", value = Steps.read.output.result) AFTER Steps.create.output
                                                                                    output
                                                                                        ifEoiDoc: System.If(condition = Arguments.isEoi) AFTER Steps.set.output
                                                                                            true
                                                                                                ifIsInternalForTrue: System.If(condition = Arguments.documentObject.isInternal) AFTER Steps.ifEoiDoc.true
                                                                                                    true
                                                                                                        ifForEoiInternal: System.If(condition = Context.updateObj.documents.eoiInternal != null) AFTER Steps.ifIsInternalForTrue.true
                                                                                                            true
                                                                                                                ifInternalEoiSigned: System.If(condition = `Context.updateObj.documents.eoiInternal.requestStatus = 'SIGNED'`) AFTER Steps.ifForEoiInternal.true
                                                                                                                    true
                                                                                                                        generateEvent6: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Document for eoi was already signed and uploaded for selected booking"
    }
}) AFTER Steps.ifInternalEoiSigned.true
                                                                                                                    false
                                                                                                                        set1: System.Context.Set(name = "Context.updateObj.documents.eoiInternal", value = Context.dataObj) AFTER Steps.ifInternalEoiSigned.false
                                                                                                                            output
                                                                                                                                updateForInternalEoi: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.updateObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.set1.output
                                                                                                                                    error
                                                                                                                                        generateEvent8: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForInternalEoi.error.result"
    }
})
                                                                                                                                    output
                                                                                                                                        generateEvent7: System.GenerateEvent(results = {
    "name": "data",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForInternalEoi.output.result"
    }
})
                                                                                                            false
                                                                                                                setInternalEoi: System.Context.Set(name = "Context.updateObj.documents.eoiInternal", value = Context.dataObj) AFTER Steps.ifForEoiInternal.false
                                                                                                                    output
                                                                                                                        updateInternalEoi: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.updateObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.setInternalEoi.output
                                                                                                                            error
                                                                                                                                generateEvent9: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateInternalEoi.error.result"
    }
})
                                                                                                                            output
                                                                                                                                generateEvent10: System.GenerateEvent(results = {
    "name": "data",
    "value": {
        "isExpression": true,
        "value": "Steps.updateInternalEoi.output.result"
    }
})
                                                                                                    false
                                                                                                        ifForEoi: System.If(condition = Context.updateObj.documents.eoi != null) AFTER Steps.ifIsInternalForTrue.false
                                                                                                            true
                                                                                                                ifEoiSigned: System.If(condition = `Context.updateObj.documents.eoi.requestStatus = 'SIGNED'`) AFTER Steps.ifForEoi.true
                                                                                                                    true
                                                                                                                        generateEvent1: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Document for eoi was already signed and uploaded for selected booking"
    }
}) AFTER Steps.ifEoiSigned.true
                                                                                                                    false
                                                                                                                        setUpdateEoiObj: System.Context.Set(value = Context.dataObj, name = "Context.updateObj.documents.eoi") AFTER Steps.ifEoiSigned.false
                                                                                                                            output
                                                                                                                                updateForEoi: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.updateObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.setUpdateEoiObj.output
                                                                                                                                    error
                                                                                                                                        generateEvent4: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForEoi.error.result"
    }
})
                                                                                                                                    output
                                                                                                                                        generateEvent5: System.GenerateEvent(results = {
    "name": "data",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForEoi.output.result"
    }
})
                                                                                                            false
                                                                                                                setNewEoiObj: System.Context.Set(name = "Context.updateObj.documents.eoi", value = Context.dataObj) AFTER Steps.ifForEoi.false
                                                                                                                    output
                                                                                                                        update: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.updateObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.setNewEoiObj.output
                                                                                                                            error
                                                                                                                                generateEvent2: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.update.error.result"
    }
})
                                                                                                                            output
                                                                                                                                generateEvent3: System.GenerateEvent(results = {
    "name": "data",
    "value": {
        "isExpression": true,
        "value": "Steps.update.output.result"
    }
})
                                                                                            false
                                                                                                ifIsInternalForFalse: System.If(condition = Arguments.documentObject.isInternal) AFTER Steps.ifEoiDoc.false
                                                                                                    true
                                                                                                        ifForOthersInternal: System.If(condition = Context.updateObj.documents.othersInternal != null) AFTER Steps.ifIsInternalForFalse.true
                                                                                                            true
                                                                                                                setLastOthersDocInt: System.Context.Set(name = `'Context.updateObj.documents.othersInternal[{{Context.updateObj.documents.othersInternal.length}}]'`, value = Context.dataObj) AFTER Steps.ifForOthersInternal.true
                                                                                                            false
                                                                                                                setFirstOthersDocInt: System.Context.Set(value = Context.dataObj, name = `'Context.updateObj.documents.othersInternal[0]'`) AFTER Steps.ifForOthersInternal.false
                                                                                                            output
                                                                                                                updateForOthersInt: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.updateObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.ifForOthersInternal.output
                                                                                                                    error
                                                                                                                        generateEvent13: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForOthersInt.error.result"
    }
})
                                                                                                                    output
                                                                                                                        generateEvent14: System.GenerateEvent(results = {
    "name": "data",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForOthersInt.output.result"
    }
})
                                                                                                    false
                                                                                                        ifForOthersExternal: System.If(condition = Context.updateObj.documents.others != null) AFTER Steps.ifIsInternalForFalse.false
                                                                                                            true
                                                                                                                setLastOthersDocExt: System.Context.Set(name = `'Context.updateObj.documents.others[{{Context.updateObj.documents.others.length}}]'`, value = Context.dataObj) AFTER Steps.ifForOthersExternal.true
                                                                                                            false
                                                                                                                setFirstOthersDocExt: System.Context.Set(name = `'Context.updateObj.documents.others[0]'`, value = Context.dataObj) AFTER Steps.ifForOthersExternal.false
                                                                                                            output
                                                                                                                updateForOthersExt: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.updateObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.ifForOthersExternal.output
                                                                                                                    error
                                                                                                                        generateEvent12: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForOthersExt.error.result"
    }
})
                                                                                                                    output
                                                                                                                        generateEvent11: System.GenerateEvent(results = {
    "name": "data",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForOthersExt.output.result"
    }
})