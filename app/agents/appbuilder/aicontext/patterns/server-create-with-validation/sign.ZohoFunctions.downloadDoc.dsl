FUNCTION downloadDoc
    NAMESPACE ZohoFunctions
    EVENTS
        output
            result AS {"version": 1, "type": "OBJECT"}
        Error
            message AS {"type": "STRING", "version": 1}
    LOGIC
        getR: CoreServices.REST.GetRequest(connectionName = "ZohoRestSign", appCode = "sign", url = "api/v1/requests/<PHONE>/pdf")
            error
                generateEvent1: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.getR.error.data"
    }
})
            output
                print: System.Print(values = Steps.getR.output.data, values = Steps.getR.error.data)
                generateEvent: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": true,
        "value": "Steps.getR.output.data"
    }
})