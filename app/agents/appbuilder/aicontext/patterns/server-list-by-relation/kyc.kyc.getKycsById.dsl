FUNCTION getKycsById
    NAMESPACE kyc
    PARAMETERS
        userId AS {"type": "INTEGER", "version": 1}
    EVENTS
        output
            kycDetails AS ARRAY OF {"ref": "kyc.Account"}
    LOGIC
        create: System.Context.Create(schema = {
    "type": "OBJECT"
}, name = "filter")
            output
                set: System.Context.Set(value = {
    "field": "userId"
}, name = "Context.filter") AFTER Steps.create.output
                    output
                        set1: System.Context.Set(value = Arguments.userId, name = "Context.filter.value") AFTER Steps.set.output
                            output
                                readPage: CoreServices.Storage.ReadPage(filter = Context.filter, appCode = "kyc", storageName = "Account") AFTER Steps.set1.output
                                    output
                                        generateEvent: System.GenerateEvent(results = {
    "name": "kycDetails",
    "value": {
        "isExpression": true,
        "value": "Steps.readPage.output.result.content"
    }
}, results = ``)