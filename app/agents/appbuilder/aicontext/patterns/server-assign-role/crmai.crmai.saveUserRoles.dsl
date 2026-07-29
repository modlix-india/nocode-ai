FUNCTION saveUserRoles
    NAMESPACE crmai
    PARAMETERS
        userRoles AS {"version": 1, "type": "OBJECT", "ref": "crmai.UserRoles"}
        clientCode AS {"type": "STRING", "version": 1}
    EVENTS
        error
            message AS {"type": "STRING", "version": 1}
        output
            userRoles AS {"type": "OBJECT", "version": 1, "ref": "crmai.UserRoles"}
    LOGIC
        create: System.Context.Create(name = "readObject", schema = {
    "ref": "crmai.UserRoles"
})
            output
                if: System.If(condition = Arguments.userRoles._id) AFTER Steps.create.output
                    true
                        update: CoreServices.Storage.Update(dataObjectId = Arguments.userRoles._id, dataObject = Arguments.userRoles, storageName = "UserRoles", clientCode = Arguments.clientCode) AFTER Steps.if.true
                            error
                                generateEvent3: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.update.error.result"
    }
}, eventName = "error")
                            output
                                generateEvent2: System.GenerateEvent(results = {
    "name": "userRoles",
    "value": {
        "isExpression": true,
        "value": "Steps.update.output.result"
    }
})
                    false
                        create1: System.Context.Create(name = "newRoles", schema = {
    "ref": "crmai.UserRoles"
}) AFTER Steps.if.false
                            output
                                set: System.Context.Set(name = "Context.newRoles", value = Arguments.userRoles) AFTER Steps.create1.output
                                    output
                                        create2: CoreServices.Storage.Create(appCode = "crmai", storageName = "UserRoles", dataObject = Context.newRoles) AFTER Steps.set.output
                                            error
                                                generateEvent1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.create2.error.result"
    }
}, eventName = "error")
                                            output
                                                generateEvent: System.GenerateEvent(results = {
    "name": "userRoles",
    "value": {
        "isExpression": true,
        "value": "Steps.create2.output.result"
    }
})