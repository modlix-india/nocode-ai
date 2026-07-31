FUNCTION getUserRoles
    NAMESPACE crmai
    EVENTS
        output
            userRoles AS {}
    LOGIC
        getAuthentication: CoreServices.SecurityContext.GetAuthentication()
            output
                if: System.If(condition = Steps.getAuthentication.output.auth)
                    true
                        readPage: CoreServices.Storage.ReadPage(storageName = "UserRoles") AFTER Steps.if.true
                            output
                                generateEvent: System.GenerateEvent(results = {
    "name": "userRoles",
    "value": {
        "isExpression": true,
        "value": "Steps.readPage.output.result.content[0]"
    }
})