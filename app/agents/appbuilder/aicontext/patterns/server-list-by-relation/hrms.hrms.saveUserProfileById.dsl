FUNCTION saveUserProfileById
    NAMESPACE hrms
    PARAMETERS
        userDetails AS {"ref": "hrms.UserProfile", "version": 1}
        userId AS {"type": "INTEGER", "version": 1}
    EVENTS
        error
            message AS {"type": "STRING", "version": 1}
        output
            userDetails AS {"ref": "hrms.UserProfile", "version": 1}
    LOGIC
        getAuthentication: CoreServices.SecurityContext.GetAuthentication()
            output
                if2: System.If(condition = Steps.getAuthentication.output.auth.loggedInFromClientId = Steps.getAuthentication.output.auth.user.clientId)
                    true
                        create: System.Context.Create(schema = {
    "ref": "hrms.UserProfile"
}, name = "readObject") AFTER Steps.if2.true
                            output
                                if: System.If(condition = Arguments.userDetails._id) AFTER Steps.create.output
                                    true
                                        read: CoreServices.Storage.Read(storageName = "UserProfile", appCode = "hrms", dataObjectId = Arguments.userDetails._id) AFTER Steps.if.true
                                            output
                                                if1: System.If(condition = Steps.read.output.result.userId = Arguments.userId)
                                                    true
                                                        update: CoreServices.Storage.Update(dataObjectId = Arguments.userDetails._id, storageName = "UserProfile", dataObject = Arguments.userDetails, appCode = "hrms") AFTER Steps.if1.true
                                                            output
                                                                generateEvent1: System.GenerateEvent(results = {
    "name": "userDetails",
    "value": {
        "isExpression": true,
        "value": "Steps.update.output.result"
    }
}, results = ``)
                                                    false
                                                        generateEvent2: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "You don't have access to update."
    }
}) AFTER Steps.if1.false
                                                            output
                                                                generateEvent3: System.GenerateEvent(results = {
    "name": "userDetails",
    "value": {
        "isExpression": false,
        "value": null
    }
}) AFTER Steps.generateEvent2.output
                                    false
                                        create1: System.Context.Create(schema = {
    "ref": "hrms.UserProfile"
}, name = "newProfile") AFTER Steps.if.false
                                            output
                                                set: System.Context.Set(value = Arguments.userDetails, name = "Context.newProfile") AFTER Steps.create1.output
                                                    output
                                                        set1: System.Context.Set(name = "Context.newProfile.userId", value = Arguments.userId) AFTER Steps.set.output
                                                            output
                                                                create2: CoreServices.Storage.Create(dataObject = Context.newProfile, storageName = "UserProfile", appCode = "hrms") AFTER Steps.set1.output
                                                                    output
                                                                        generateEvent: System.GenerateEvent(results = {
    "name": "userDetails",
    "value": {
        "isExpression": true,
        "value": "Steps.create2.output.result"
    }
})
                    false
                        generateEvent4: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "You don't have access to update."
    }
}) AFTER Steps.if2.false