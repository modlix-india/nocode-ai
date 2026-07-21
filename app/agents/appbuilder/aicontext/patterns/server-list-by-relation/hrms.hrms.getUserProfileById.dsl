FUNCTION getUserProfileById
    NAMESPACE hrms
    PARAMETERS
        userId AS {"type": "LONG", "version": 1}
        userCode AS {"type": "STRING", "version": 1, "defaultValue": ""}
    EVENTS
        output
            userProfile AS {"type": "OBJECT", "version": 1, "ref": "hrms.userProfile"}
        error
            message AS {"type": "STRING", "version": 1}
    LOGIC
        create: System.Context.Create(name = "filter", schema = {
    "type": "OBJECT"
})
            output
                set: System.Context.Set(name = "Context.filter", value = {
    "field": "userId"
}) AFTER Steps.create.output
                    output
                        set1: System.Context.Set(name = "Context.filter.value", value = Arguments.userId) AFTER Steps.set.output
                            output
                                readPage: CoreServices.Storage.ReadPage(storageName = "UserProfile", appCode = "hrms", filter = Context.filter, clientCode = Arguments.userCode) AFTER Steps.set1.output
                                    output
                                        if: System.If(condition = Steps.readPage.output.result.content.length > 0)
                                            true
                                                generateEvent1: System.GenerateEvent(results = {
    "name": "userProfile",
    "value": {
        "isExpression": true,
        "value": "Steps.readPage.output.result"
    }
}) AFTER Steps.if.true
                                            false
                                                generateEvent: System.GenerateEvent(results = {
    "name": "userProfile",
    "value": {
        "isExpression": false,
        "value": {}
    }
}, results = ``) AFTER Steps.if.false