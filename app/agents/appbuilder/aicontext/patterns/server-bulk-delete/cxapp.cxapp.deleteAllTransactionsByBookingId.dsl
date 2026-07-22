FUNCTION deleteAllTransactionsByBookingId
    NAMESPACE cxapp
    PARAMETERS
        bookingId AS {"type": "STRING", "version": 1}
    EVENTS
        output
            result AS {"type": "OBJECT", "version": 1}
    LOGIC
        create: System.Context.Create(schema = {
    "type": "OBJECT"
}, name = "filter")
            output
                set: System.Context.Set(name = "Context.filter", value = {
    "field": "bookingId",
    "value": ""
}) AFTER Steps.create.output
                    output
                        set1: System.Context.Set(name = "Context.filter.value", value = Arguments.bookingId) AFTER Steps.set.output
                            output
                                deleteByFilter: CoreServices.Storage.DeleteByFilter(devMode = false, filter = Context.filter, storageName = "paymentDetails", appCode = "cxapp") AFTER Steps.set1.output
                                    output
                                        generateEvent: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": true,
        "value": "Steps.deleteByFilter.output.result"
    }
})