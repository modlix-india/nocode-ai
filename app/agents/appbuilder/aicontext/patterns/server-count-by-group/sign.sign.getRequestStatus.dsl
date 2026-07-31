FUNCTION getRequestStatus
    NAMESPACE sign
    PARAMETERS
        request_id AS {"type": "STRING", "version": 1}
    EVENTS
        output
            result AS {"type": "OBJECT", "version": 1}
        error
            message AS {"type": "STRING", "version": 1}
    LOGIC
        getRequest: CoreServices.REST.GetRequest(connectionName = "ZohoRestSign", appCode = "sign", url = `'/api/v1/requests/{{Arguments.request_id}}'`)
            error
                generateEvent2: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.getRequest.error.data"
    }
})
            output
                generateEvent: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": true,
        "value": "Steps.getRequest.output.data"
    }
})