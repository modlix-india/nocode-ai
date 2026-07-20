FUNCTION updateStatus
    NAMESPACE rim
    PARAMETERS
        bookingId AS {"type": "STRING", "version": 1}
        delete AS {"type": "BOOLEAN", "version": 1}
        documentId AS {"type": "STRING", "version": 1}
        isEoi AS {"type": "BOOLEAN", "version": 1}
        isInternal AS {"type": "BOOLEAN", "version": 1}
        agreementsId AS {"type": "INTEGER", "version": 1}
        legalsId AS {"type": "INTEGER", "version": 1}
        updateStatus AS {"type": "BOOLEAN", "version": 1, "defaultValue": false}
        isBookingForm AS {"type": "BOOLEAN", "version": 1}
        isVBF AS {"type": "BOOLEAN", "version": 1}
        isEOI AS {"type": "BOOLEAN", "version": 1}
        isUNITEOI AS {"type": "BOOLEAN", "version": 1}
        isPBF AS {"type": "BOOLEAN", "version": 1}
    EVENTS
        error
            message AS {"type": "STRING", "version": 1}
        output
            document AS {"type": "OBJECT", "version": 1}
    LOGIC
        create3: System.Context.Create(name = "generatingStatus", schema = {
    "type": "OBJECT"
})
            output
                set9: System.Context.Set(name = "Context.generatingStatus", value = {
    "bookingForm": "bookingForm Generating",
    "VBF": "bookingForm Generating",
    "eoi": "EOI Generating",
    "EOI": "EOI Generating",
    "UNITEOI": "EOI Generating",
    "PBF": "bookingForm Generating"
}) AFTER Steps.create3.output
                    output
                        create3_Copy_1: System.Context.Create(name = "signedStatus", schema = {
    "type": "OBJECT"
}) AFTER Steps.set9.output
                            output
                                set9_Copy_1: System.Context.Set(name = "Context.signedStatus", value = {
    "bookingForm": "Booking Form Signed",
    "VBF": "Booking Form Signed",
    "eoi": "EOI Signed",
    "EOI": "EOI Signed",
    "UNITEOI": "EOI Signed",
    "PBF": "Booking Form Signed"
}) AFTER Steps.create3_Copy_1.output
                                    output
                                        create_Copy_1: System.Context.Create(schema = {
    "ref": "rim.Bookings.BookingDetails"
}, name = "bookingObj") AFTER Steps.set9_Copy_1.output
                                            output
                                                read: CoreServices.Storage.Read(appCode = "rim", storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.create_Copy_1.output
                                                    error
                                                        generateEvent: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.read.error.result"
    }
}, eventName = "error")
                                                    output
                                                        create1: System.Context.Create(name = "docsKeysList", schema = {
    "type": "ARRAY"
}) AFTER Steps.read.output
                                                            output
                                                                create: System.Context.Create(name = "docsStatus", schema = {
    "type": "OBJECT"
}) AFTER Steps.create1.output
                                                                    output
                                                                        create2: System.Context.Create(name = "docIsThere", schema = {
    "type": "BOOLEAN"
}) AFTER Steps.create.output
                                                                            output
                                                                                set8: System.Context.Set(name = "Context.docIsThere", value = `false`) AFTER Steps.create2.output
                                                                                    output
                                                                                        set: System.Context.Set(name = "Context.docsStatus", value = {
    "bookingForm": false,
    "VBF": false,
    "eoi": false,
    "EOI": false,
    "UNITEOI": false,
    "PBF": false
}) AFTER Steps.set8.output
                                                                                            output
                                                                                                set10: System.Context.Set(name = "Context.docsStatus.PBF", value = Arguments.isPBF) AFTER Steps.set.output
                                                                                                    output
                                                                                                        set2: System.Context.Set(name = "Context.docsStatus.bookingForm", value = Arguments.isBookingForm) AFTER Steps.set10.output
                                                                                                            output
                                                                                                                set3: System.Context.Set(name = "Context.docsStatus.VBF", value = Arguments.isVBF) AFTER Steps.set2.output
                                                                                                                    output
                                                                                                                        set4: System.Context.Set(name = "Context.docsStatus.eoi", value = Arguments.isEoi) AFTER Steps.set3.output
                                                                                                                            output
                                                                                                                                set5: System.Context.Set(name = "Context.docsStatus.EOI", value = Arguments.isEOI) AFTER Steps.set4.output
                                                                                                                                    output
                                                                                                                                        set6: System.Context.Set(name = "Context.docsStatus.UNITEOI", value = Arguments.isUNITEOI) AFTER Steps.set5.output
                                                                                                                                            output
                                                                                                                                                objectKeys: System.Object.ObjectKeys(source = Context.docsStatus) AFTER Steps.set6.output
                                                                                                                                                    output
                                                                                                                                                        set1: System.Context.Set(name = "Context.docsKeysList", value = Steps.objectKeys.output.value)
                                                                                                                                                            output
                                                                                                                                                                forEachLoop: System.Loop.ForEachLoop(source = Context.docsKeysList) AFTER Steps.set1.output
                                                                                                                                                                    iteration
                                                                                                                                                                        if: System.If(condition = Context.docsStatus.{{Steps.forEachLoop.iteration.each}} = true)
                                                                                                                                                                            true
                                                                                                                                                                                set7: System.Context.Set(name = "Context.docIsThere", value = `true`) AFTER Steps.if.true
                                                                                                                                                                                    output
                                                                                                                                                                                        set_Copy_1: System.Context.Set(value = Steps.read.output.result , name = "Context.bookingObj") AFTER Steps.set7.output
                                                                                                                                                                                            output
                                                                                                                                                                                                ifBookingFormTrueIsInternal: System.If(condition = Arguments.isInternal) AFTER Steps.set_Copy_1.output
                                                                                                                                                                                                    true
                                                                                                                                                                                                        ifForBookingFormInternalUpdate: System.If(condition = Context.bookingObj.documents.{{Steps.forEachLoop.iteration.each}}.documentId = Arguments.documentId) AFTER Steps.ifBookingFormTrueIsInternal.true
                                                                                                                                                                                                            true
                                                                                                                                                                                                                ifForEoiInternalDelete_Copy_1: System.If(condition = Arguments.delete) AFTER Steps.ifForBookingFormInternalUpdate.true
                                                                                                                                                                                                                    true
                                                                                                                                                                                                                        objectDeleteKeyInternalBF: System.Object.ObjectDeleteKey(key = Steps.forEachLoop.iteration.each, source = Context.bookingObj.documents) AFTER Steps.ifForEoiInternalDelete_Copy_1.true
                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                set2_Copy_1: System.Context.Set(value = Steps.objectDeleteKeyInternalBF.output.value, name = "Context.bookingObj.documents")
                                                                                                                                                                                                                    output
                                                                                                                                                                                                                        updateForBookingFormInternal: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.bookingObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.ifForEoiInternalDelete_Copy_1.output
                                                                                                                                                                                                                            error
                                                                                                                                                                                                                                generateEvent6_Copy_1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForBookingFormInternal.error.result"
    }
}, eventName = "error")
                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                generateEvent5_Copy_1: System.GenerateEvent(results = {
    "name": "document",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForBookingFormInternal.output.result"
    }
})
                                                                                                                                                                                                            false
                                                                                                                                                                                                                generateEvent4_Copy_1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Please provide valid document"
    }
}, eventName = "error") AFTER Steps.ifForBookingFormInternalUpdate.false
                                                                                                                                                                                                    false
                                                                                                                                                                                                        ifBookingFormUpdate: System.If(condition = Context.bookingObj.documents.{{Steps.forEachLoop.iteration.each}}.documentId = Arguments.documentId) AFTER Steps.ifBookingFormTrueIsInternal.false
                                                                                                                                                                                                            true
                                                                                                                                                                                                                ifBookingFormDelete: System.If(condition = Arguments.delete) AFTER Steps.ifBookingFormUpdate.true
                                                                                                                                                                                                                    true
                                                                                                                                                                                                                        objectDeleteKeyBF: System.Object.ObjectDeleteKey(key = Steps.forEachLoop.iteration.each, source = Context.bookingObj.documents) AFTER Steps.ifBookingFormDelete.true
                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                set1_Copy_1: System.Context.Set(value = Steps.objectDeleteKeyBF.output.value, name = "Context.bookingObj.documents")
                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                        setStatusDefault_Copy_1: System.Context.Set(name = "Context.bookingObj.status", value = Context.generatingStatus.{{Steps.forEachLoop.iteration.each}}) AFTER Steps.set1_Copy_1.output
                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                setPurchaseStatusDefault_Copy_1: System.Context.Set(name = "Context.bookingObj.purchaseStatus", value = "Active") AFTER Steps.setStatusDefault_Copy_1.output
                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                        updateStatusesBF: CoreServices.Storage.Update(dataObject = Context.bookingObj, storageName = "Bookings", dataObjectId = Arguments.bookingId, appCode = "rim") AFTER Steps.setPurchaseStatusDefault_Copy_1.output
                                                                                                                                                                                                                                                            error
                                                                                                                                                                                                                                                                generateEvent18_Copy_1: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateStatusesBF.error.result"
    }
})
                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                generateEvent17_Copy_1: System.GenerateEvent(results = {
    "name": "document",
    "value": {
        "isExpression": true,
        "value": "Steps.updateStatusesBF.output.result"
    }
})
                                                                                                                                                                                                                    false
                                                                                                                                                                                                                        ifChangeStatus_Copy_1: System.If(condition = Arguments.updateStatus) AFTER Steps.ifBookingFormDelete.false
                                                                                                                                                                                                                            true
                                                                                                                                                                                                                                ifEoiSigned_Copy_1: System.If(condition = `Context.bookingObj.documents.{{Steps.forEachLoop.iteration.each}}.requestStatus = "NOT_SIGNED"`) AFTER Steps.ifChangeStatus_Copy_1.true
                                                                                                                                                                                                                                    true
                                                                                                                                                                                                                                        changeRequestStatus_Copy_1: System.Context.Set(name = `'Context.bookingObj.documents.{{Steps.forEachLoop.iteration.each}}.requestStatus'`, value = "SIGNED") AFTER Steps.ifEoiSigned_Copy_1.true
                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                setStatus_Copy_1: System.Context.Set(name = "Context.bookingObj.status", value = Context.signedStatus.{{Steps.forEachLoop.iteration.each}}) AFTER Steps.changeRequestStatus_Copy_1.output
                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                        setPurchaseStatus_Copy_1: System.Context.Set(name = "Context.bookingObj.purchaseStatus", value = "Active") AFTER Steps.setStatus_Copy_1.output
                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                updateBookingFormRequestStatus: CoreServices.Storage.Update(dataObjectId = Arguments.bookingId, appCode = "rim", dataObject = Context.bookingObj, storageName = "Bookings") AFTER Steps.setPurchaseStatus_Copy_1.output
                                                                                                                                                                                                                                                                    error
                                                                                                                                                                                                                                                                        generateEvent16_Copy_1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateBookingFormRequestStatus.error.result"
    }
}, eventName = "error")
                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                        generateEvent15_Copy_1: System.GenerateEvent(results = {
    "name": "document",
    "value": {
        "isExpression": true,
        "value": "Steps.updateBookingFormRequestStatus.output.result"
    }
})
                                                                                                                                                                                                                                    false
                                                                                                                                                                                                                                        generateEvent14_Copy_1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Document already signed"
    }
}, eventName = "error") AFTER Steps.ifEoiSigned_Copy_1.false
                                                                                                                                                                                                                    output
                                                                                                                                                                                                                        updateForBookingForm: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.bookingObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.ifBookingFormDelete.output
                                                                                                                                                                                                                            error
                                                                                                                                                                                                                                generateEvent3_Copy_1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForBookingForm.error.result"
    }
}, eventName = "error")
                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                generateEvent2_Copy_1: System.GenerateEvent(results = {
    "name": "document",
    "value": {
        "isExpression": true,
        "value": "Steps.updateForBookingForm.output.result"
    }
})
                                                                                                                                                                                                            false
                                                                                                                                                                                                                generateEvent1_Copy_1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Please provide valid document"
    }
}) AFTER Steps.ifBookingFormUpdate.false
                                                                                                                                                                    output
                                                                                                                                                                        if1: System.If(condition = Context.docIsThere = false) AFTER Steps.forEachLoop.output
                                                                                                                                                                            true
                                                                                                                                                                                ifForAgreements: System.If(condition = Arguments.agreementsId = -1) AFTER Steps.if1.true
                                                                                                                                                                                    true
                                                                                                                                                                                        ifForLegal: System.If(condition = Arguments.legalsId = -1) AFTER Steps.ifForAgreements.true
                                                                                                                                                                                            true
                                                                                                                                                                                                generateEvent7: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Please provide others document location id"
    }
}, eventName = "error") AFTER Steps.ifForLegal.true
                                                                                                                                                                                            false
                                                                                                                                                                                                set5_Copy_1: System.Context.Set(value = Steps.read.output.result, name = `'Context.bookingObj'`) AFTER Steps.ifForLegal.false
                                                                                                                                                                                                    output
                                                                                                                                                                                                        ifLegalUpdate: System.If(condition = `'Context.bookingObj.documents.legal[{{Arguments.legalsId}}].documentId = Arguments.documentId'`) AFTER Steps.set5_Copy_1.output
                                                                                                                                                                                                            true
                                                                                                                                                                                                                ifLegalDelete: System.If(condition = Arguments.delete) AFTER Steps.ifLegalUpdate.true
                                                                                                                                                                                                                    true
                                                                                                                                                                                                                        delete1: System.Array.Delete(source = Context.bookingObj.documents.legal, element = Context.bookingObj.documents.legal[{{Arguments.legalsId}}]) AFTER Steps.ifLegalDelete.true
                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                set6_Copy_1: System.Context.Set(value = Steps.delete1.output.result, name = "Context.bookingObj.documents.legal")
                                                                                                                                                                                                                    output
                                                                                                                                                                                                                        update1: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.bookingObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.ifLegalDelete.output
                                                                                                                                                                                                                            error
                                                                                                                                                                                                                                generateEvent13: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.update1.error.result"
    }
}, eventName = "error")
                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                generateEvent12: System.GenerateEvent(results = {
    "name": "document",
    "value": {
        "isExpression": true,
        "value": "Steps.update1.output.result"
    }
})
                                                                                                                                                                                                            false
                                                                                                                                                                                                                generateEvent11: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Please provide valid document"
    }
}, eventName = "error") AFTER Steps.ifLegalUpdate.false
                                                                                                                                                                                    false
                                                                                                                                                                                        set3_Copy_1: System.Context.Set(value = Steps.read.output.result, name = "Context.bookingObj") AFTER Steps.ifForAgreements.false
                                                                                                                                                                                            output
                                                                                                                                                                                                ifAgreementsUpdate: System.If(condition = `'Context.bookingObj.documents.agreements[{{Arguments.agreementsId}}].documentId = Arguments.documentId'`) AFTER Steps.set3_Copy_1.output
                                                                                                                                                                                                    true
                                                                                                                                                                                                        ifAgreementsDelete: System.If(condition = Arguments.delete) AFTER Steps.ifAgreementsUpdate.true
                                                                                                                                                                                                            true
                                                                                                                                                                                                                delete: System.Array.Delete(source = Context.bookingObj.documents.agreements, element = Context.bookingObj.documents.agreements[{{Arguments.agreementsId}}]) AFTER Steps.ifAgreementsDelete.true
                                                                                                                                                                                                                    output
                                                                                                                                                                                                                        set4_Copy_1: System.Context.Set(value = Steps.delete.output.result, name = "Context.bookingObj.documents.agreements")
                                                                                                                                                                                                            false
                                                                                                                                                                                                                changingRequestStatus: System.Context.Set(name = "Context.bookingObj.documents.agreements[{{Arguments.agreementsId}}].requestStatus", value = "SIGNED") AFTER Steps.ifAgreementsDelete.false
                                                                                                                                                                                                            output
                                                                                                                                                                                                                update: CoreServices.Storage.Update(appCode = "rim", dataObject = Context.bookingObj, storageName = "Bookings", dataObjectId = Arguments.bookingId) AFTER Steps.ifAgreementsDelete.output
                                                                                                                                                                                                                    error
                                                                                                                                                                                                                        generateEvent10: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.update.error.result"
    }
}, eventName = "error")
                                                                                                                                                                                                                    output
                                                                                                                                                                                                                        generateEvent9: System.GenerateEvent(results = {
    "name": "document",
    "value": {
        "isExpression": true,
        "value": "Steps.update.output.result"
    }
})
                                                                                                                                                                                                    false
                                                                                                                                                                                                        generateEvent8: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "Please provide valid document"
    }
}, eventName = "error") AFTER Steps.ifAgreementsUpdate.false