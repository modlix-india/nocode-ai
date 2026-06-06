FUNCTION MetaLeadFormsFetch
    NAMESPACE MarketingAI
    PARAMETERS
        pageId AS {}
        page AS {}
        pageSize AS {}
    EVENTS
        output
            leadforms AS {}
            pageDetails AS {}
        error
            message AS {}
    LOGIC
        create1: System.Context.Create(schema = {
    "type": "OBJECT"
}, name = "fetchedPages")
            output
                getRequest: CoreServices.REST.GetRequest(url = `'/me/accounts?fields=name,id,access_token,picture'`, connectionName = "META_API", appCode = "marketingai") AFTER Steps.create1.output
                    error
                        set2: System.Context.Set(name = "Context.fetchedPages.error", value = Steps.getRequest.error.data)
                    output
                        set1: System.Context.Set(name = "Context.fetchedPages.pages", value = Steps.getRequest.output.data)
                            output
                                if: System.If(condition = Context.fetchedPages.error) AFTER Steps.set1.output
                                    true
                                        generateEvent2: System.GenerateEvent(results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Context.fetchedPages.error"
    }
}, eventName = "error") AFTER Steps.if.true
                                    false
                                        create: System.Context.Create(name = "businessPage", schema = {
    "type": "OBJECT"
}) AFTER Steps.if.false
                                            output
                                                set: System.Context.Set(name = "Context.businessPage.pageId", value = Arguments.pageId) AFTER Steps.create.output
                                                    output
                                                        create2: System.Context.Create(name = "PageDetails", schema = {
    "type": "OBJECT"
}) AFTER Steps.set.output
                                                            output
                                                                forEachLoop: System.Loop.ForEachLoop(source = Context.fetchedPages.pages.data) AFTER Steps.create2.output
                                                                    iteration
                                                                        checkingPageId: System.If(condition = Steps.forEachLoop.iteration.each.id = Context.businessPage.pageId)
                                                                            true
                                                                                set3: System.Context.Set(name = "Context.businessPage.access_token", value = Steps.forEachLoop.iteration.each.access_token) AFTER Steps.checkingPageId.true
                                                                                    output
                                                                                        set4: System.Context.Set(name = "Context.PageDetails.name", value = Steps.forEachLoop.iteration.each.name) AFTER Steps.set3.output
                                                                                            output
                                                                                                set5: System.Context.Set(value = Steps.forEachLoop.iteration.each.id, name = "Context.PageDetails.id") AFTER Steps.set4.output
                                                                                                    output
                                                                                                        set6: System.Context.Set(name = "Context.PageDetails.picture", value = Steps.forEachLoop.iteration.each.picture) AFTER Steps.set5.output
                                                                                                            output
                                                                                                                break: System.Loop.Break(stepName = `'forEachLoop'`) AFTER Steps.set6.output
                                                                    output
                                                                        getRequest1: CoreServices.REST.GetRequest(url = `"{{Context.businessPage.pageId}}/leadgen_forms?fields=name,id,status,created_time,leads_count,is_optimized_for_quality,question_page_custom_headline,questions,thank_you_page,legal_content,context_card&limit={{Arguments.pageSize}}&access_token={{Context.businessPage.access_token}}&{{Arguments.page}}"`, appCode = "marketingai", connectionName = "META_API", queryParams = {}) AFTER Steps.forEachLoop.output
                                                                            error
                                                                                set7: System.Context.Set(name = "Context.businessPage.error", value = Steps.getRequest1.error.data)
                                                                            output
                                                                                set8: System.Context.Set(name = "Context.businessPage.leadForms", value = Steps.getRequest1.output.data)
                                                                                    output
                                                                                        if1: System.If(condition = Context.businessPage.error) AFTER Steps.set8.output
                                                                                            true
                                                                                                generateEvent1: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Context.businessPage.error"
    }
}) AFTER Steps.if1.true
                                                                                            false
                                                                                                generateEvent: System.GenerateEvent(results = {
    "name": "leadforms",
    "value": {
        "isExpression": true,
        "value": "Context.businessPage.leadForms"
    }
}, results = {
    "name": "pageDetails",
    "value": {
        "isExpression": true,
        "value": "Context.PageDetails"
    }
}) AFTER Steps.if1.false