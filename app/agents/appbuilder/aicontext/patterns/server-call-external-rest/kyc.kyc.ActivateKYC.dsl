FUNCTION ActivateKYC
    NAMESPACE kyc
    PARAMETERS
        kyc AS {"ref": "kyc.Account", "version": 1}
    EVENTS
        error
            message AS {"type": "STRING", "version": 1}
        output
            kycDetails AS {"ref": "kyc.Account", "version": 1}
    LOGIC
        create: System.Context.Create(name = "readObject", schema = {
    "ref": "kyc.Account"
})
            output
                if: System.If(condition = Arguments.kyc._id) AFTER Steps.create.output
                    true
                        read: CoreServices.Storage.Read(dataObjectId = Arguments.kyc._id, storageName = `'Account'`, appCode = "kyc") AFTER Steps.if.true
                            output
                                set: System.Context.Set(name = "Context.readObject", value = Steps.read.output.result)
                                    output
                                        set1: System.Context.Set(name = "Context.readObject.status", value = `'VERIFIED'`) AFTER Steps.set.output
                    output
                        context: CoreServices.SecurityContext.GetAuthentication() AFTER Steps.if.output
                            output
                                if1: System.If(condition = Context.readObject.userId = Steps.context.output.auth.user.id)
                                    true
                                        update: CoreServices.Storage.Update(storageName = "Account", dataObjectId = Arguments.kyc._id, appCode = "kyc", dataObject = Context.readObject) AFTER Steps.if1.true
                                            output
                                                generateEvent: System.GenerateEvent(results = {
    "name": "kycDetails",
    "value": {
        "isExpression": true,
        "value": "Steps.update.output.result"
    }
})
                                    false
                                        if2: System.If(condition = Steps.context.output.auth.loggedInFromClientId = Steps.context.output.auth.user.clientId) AFTER Steps.if1.false
                                            true
                                                update1: CoreServices.Storage.Update(appCode = "kyc", dataObject = Context.readObject, storageName = "Account", dataObjectId = Arguments.kyc._id) AFTER Steps.if2.true
                                                    output
                                                        generateEvent3: System.GenerateEvent(results = {
    "name": "kycDetails",
    "value": {
        "isExpression": true,
        "value": "Steps.update1.output.result"
    }
})
                                            false
                                                generateEvent1: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": false,
        "value": "You don't have access to update."
    }
}) AFTER Steps.if2.false
                                                    output
                                                        generateEvent2: System.GenerateEvent(results = {
    "name": "kycDetails",
    "value": {
        "isExpression": false,
        "value": null
    }
}) AFTER Steps.generateEvent1.output