FUNCTION saveIncomingMessages
    NAMESPACE crmai
    PARAMETERS
        entry AS {"type": "ARRAY", "version": 1}
        object AS {"type": "STRING", "version": 1}
    EVENTS
        output
            result AS {"type": "STRING", "version": 1}
    LOGIC
        create: System.Context.Create(schema = {
    "type": "OBJECT"
}, name = "incomingObject")
            output
                set: System.Context.Set(value = Arguments.entry, name = "Context.incomingObject.entry") AFTER Steps.create.output
                    output
                        set1: System.Context.Set(value = Arguments.object, name = "Context.incomingObject.object") AFTER Steps.set.output
                            output
                                if: System.If(condition = Context.incomingObject.entry[0].changes[0].value.statuses) AFTER Steps.set1.output
                                    true
                                        updateReadReceipt: crmai.updateReadReceipt(incomingObject = Context.incomingObject) AFTER Steps.if.true
                                            output
                                                generateEvent: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": false,
        "value": "200 OK HTTPS"
    }
}) AFTER Steps.updateReadReceipt.output
                                    false
                                        if1: System.If(condition = `Context.incomingObject.entry[0].changes[0].value.messages[0].type = "text"`) AFTER Steps.if.false
                                            true
                                                saveNewMessage: crmai.saveNewMessage(incomingObject = Context.incomingObject) AFTER Steps.if1.true
                                                    output
                                                        generateEvent1: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": false,
        "value": "200 OK HTTPS"
    }
}) AFTER Steps.saveNewMessage.output
                                            false
                                                generateEvent2: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": false,
        "value": "Coming soon"
    }
}) AFTER Steps.if1.false