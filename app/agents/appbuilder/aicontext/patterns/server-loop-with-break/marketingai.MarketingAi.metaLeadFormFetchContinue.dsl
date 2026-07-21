FUNCTION metaLeadFormFetchContinue
    NAMESPACE MarketingAi
    PARAMETERS
        pageId AS {}
    EVENTS
        error
            message AS {}
        output
            leadforms AS {}
    LOGIC
        forEachLoop: System.Loop.ForEachLoop(source = Context.fetchedPages.pages)
            iteration
                if: System.If(condition = Steps.forEachLoop.iteration.each.id = Context.businessPage.pageId)
                    true
                        set: System.Context.Set(name = "Context.businessPage.access_token", value = Steps.forEachLoop.iteration.each.access_token) AFTER Steps.if.true
                            output
                                break: System.Loop.Break(stepName = `'forEachLoop'`) AFTER Steps.set.output
            output
                getRequest: CoreServices.REST.GetRequest(appCode = "marketingai", url = `'/{{Context.businessPage.pageId}}/leadgen_forms?fields=id,name,created_time,leads_count,status&access_token={{Context.businessPage.access_token}}'`, connectionName = "META_API") AFTER Steps.forEachLoop.output
                    error
                        generateEvent1: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.getRequest.error.data"
    }
}, eventName = "error")
                    output
                        generateEvent: System.GenerateEvent(results = {
    "name": "leadforms",
    "value": {
        "isExpression": true,
        "value": "Steps.getRequest.output.data"
    }
})