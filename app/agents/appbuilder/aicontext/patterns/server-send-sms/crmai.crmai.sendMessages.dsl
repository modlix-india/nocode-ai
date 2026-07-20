FUNCTION sendMessages
    NAMESPACE crmai
    PARAMETERS
        message AS {"type": "OBJECT", "version": 1}
        messageType AS {"type": "STRING", "version": 1}
        phoneNumberId AS {"type": "STRING", "version": 1}
    EVENTS
        output
            message AS {"type": "OBJECT", "version": 1}
    LOGIC
        postRequest: CoreServices.REST.PostRequest(headers = null, appCode = "crmai", url = `'{{Arguments.phoneNumberId}}/{{Arguments.messageType}}'`, payload = Arguments.message, connectionName = "meta_Connection")
            error
                generateEvent1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.postRequest.error.data"
    }
})
            output
                generateEvent: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.postRequest.output.data"
    }
})