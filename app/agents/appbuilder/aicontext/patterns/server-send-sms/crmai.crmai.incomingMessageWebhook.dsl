FUNCTION incomingMessageWebhook
    NAMESPACE crmai
    PARAMETERS
        event_details AS {"type": "OBJECT", "version": 1}
        call_details AS {"type": "OBJECT", "version": 1}
    EVENTS
        output
            result AS {"type": "STRING", "version": 1}
    LOGIC
        create: System.Context.Create(name = "message", schema = {
    "type": "OBJECT"
})
            output
                set: System.Context.Set(name = "Context.message.event_details", value = Arguments.event_details) AFTER Steps.create.output
                    output
                        set1: System.Context.Set(name = "Context.message.call_details", value = Arguments.call_details) AFTER Steps.set.output
                            output
                                create2: CoreServices.Storage.Create(storageName = "testWeb", dataObject = Context.message, appCode = "crmai") AFTER Steps.set1.output
                                    output
                                        generateEvent1: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": false,
        "value": "200 OK HTTPS"
    }
}) AFTER Steps.create2.output