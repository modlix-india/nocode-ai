FUNCTION sendEmail
    NAMESPACE leadzump
    PARAMETERS
        payload AS {"type": "OBJECT", "version": 1}
        connectionName AS {"version": 1, "type": "STRING"}
        url AS {"type": "STRING", "version": 1}
    EVENTS
        output
            response AS {"type": "OBJECT", "version": 1}
    LOGIC
        create: System.Context.Create(name = "object", schema = {
    "type": "OBJECT"
})
            output
                if: System.If(condition = `Arguments.url = 'api/generatePdfFromTemplate/webhook/data'`) AFTER Steps.create.output
                    true
                        set1: System.Context.Set(name = "Context.object", value = Arguments.payload) AFTER Steps.if.true
                    false
                        set: System.Context.Set(name = "Context.object.details", value = Arguments.payload) AFTER Steps.if.false
                    output
                        postRequest: CoreServices.REST.PostRequest(url = Arguments.url, payload = Context.object, connectionName = Arguments.connectionName) AFTER Steps.if.output
                            output
                                generateEvent: System.GenerateEvent(results = {
    "name": "response",
    "value": {
        "isExpression": true,
        "value": "Steps.postRequest.output.data"
    }
}) AFTER Steps.postRequest.output