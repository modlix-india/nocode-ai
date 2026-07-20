FUNCTION embeddedUrl
    NAMESPACE sign
    PARAMETERS
        request_id AS {"type": "STRING", "version": 1}
        action_id AS {"type": "STRING", "version": 1}
        hostName AS {"type": "STRING", "version": 1}
    EVENTS
        error
            message AS {}
        output
            embeddedurl AS {"ref": "sign.EmbeddedResponse", "version": 1}
    LOGIC
        getAuthentication: CoreServices.SecurityContext.GetAuthentication()
            output
                if: System.If(condition = Steps.getAuthentication.output.auth.loggedInFromClientId = Steps.getAuthentication.output.auth.user.clientId)
                    true
                        generateEvent2: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "You don't access to trigger the embedded url"
    }
}) AFTER Steps.if.true
                    false
                        postRequest: CoreServices.REST.PostRequest(appCode = "sign", connectionName = "ZohoRestSign", url = `"api/v1/requests/{{Arguments.request_id}}/actions/{{Arguments.action_id}}/embedtoken?host={{Arguments.hostName}}"`, payload = ``) AFTER Steps.if.false
                            error
                                generateEvent1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.postRequest.error.data"
    }
}, eventName = "error")
                            output
                                ifFailed: System.If(condition = Steps.postRequest.output.statusCode >= 400)
                                    true
                                        generateEvent3: System.GenerateEvent(eventName = "error", results = {
    "name": "embeddedurl",
    "value": {
        "isExpression": true,
        "value": "Steps.postRequest.output.data.message"
    }
}) AFTER Steps.ifFailed.true
                                    false
                                        generateEvent: System.GenerateEvent(results = {
    "name": "embeddedurl",
    "value": {
        "isExpression": true,
        "value": "Steps.postRequest.output.data"
    }
}) AFTER Steps.ifFailed.false