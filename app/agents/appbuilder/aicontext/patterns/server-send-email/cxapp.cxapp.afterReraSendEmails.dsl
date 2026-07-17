FUNCTION afterReraSendEmails
    NAMESPACE cxapp
    PARAMETERS
        filterObject AS {"type": "OBJECT", "version": 1}
        pageNumber AS {"type": "INTEGER", "version": 1}
    EVENTS
        result
            response AS {"type": "OBJECT", "version": 1}
    LOGIC
        readPage: CoreServices.Storage.ReadPage(storageName = "Bookings", filter = Arguments.filterObject, appCode = "rim", page = Arguments.pageNumber)
            output
                generateEvent: System.GenerateEvent(results = {
    "name": "response",
    "value": {
        "isExpression": true,
        "value": "Steps.readPage.output.result"
    }
}, eventName = "result")