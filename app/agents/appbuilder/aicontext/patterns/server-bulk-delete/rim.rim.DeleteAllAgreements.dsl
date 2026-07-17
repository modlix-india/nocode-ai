FUNCTION DeleteAllAgreements
    NAMESPACE rim
    PARAMETERS
        bookingId AS {"type": "STRING", "version": 1}
    EVENTS
        output
            response AS {"type": "OBJECT", "version": 1}
        error
            message AS {"type": "STRING", "version": 1}
    LOGIC
        fitler: System.Context.Create(name = "filter", schema = {
    "type": "OBJECT"
})
            output
                set: System.Context.Set(name = "Context.filter", value = {
    "operator": "AND",
    "conditions": [
        {
            "field": "bookingId",
            "value": ""
        },
        {
            "field": "docType",
            "value": "agreements"
        }
    ]
}) AFTER Steps.fitler.output
                    output
                        value: System.Context.Set(name = "Context.filter.conditions[0].value", value = Arguments.bookingId) AFTER Steps.set.output
                            output
                                deleteByFilter: CoreServices.Storage.DeleteByFilter(filter = Context.filter, storageName = "Documents", appCode = "rim") AFTER Steps.value.output
                                    error
                                        generateEvent: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.deleteByFilter.error.result"
    }
}, eventName = "error")
                                    output
                                        fitler_Copy_1: System.Context.Create(name = "booking", schema = {
    "type": "OBJECT"
}) AFTER Steps.deleteByFilter.output
                                            output
                                                set1: System.Context.Set(value = {
    "documents": {
        "agreements": []
    }
}, name = "Context.booking") AFTER Steps.fitler_Copy_1.output
                                                    output
                                                        updateBookingDetailsByBookingId: rim.updateBookingDetailsByBookingId(bookingId = Arguments.bookingId, bookingObject = Context.booking) AFTER Steps.set1.output
                                                            error
                                                                generateEvent_Copy_1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.updateBookingDetailsByBookingId.error.message"
    }
}, eventName = "error")
                                                            output
                                                                generateEvent1: System.GenerateEvent(results = {
    "name": "response",
    "value": {
        "isExpression": true,
        "value": "Steps.updateBookingDetailsByBookingId.output.bookingDetails"
    }
}) AFTER Steps.updateBookingDetailsByBookingId.output