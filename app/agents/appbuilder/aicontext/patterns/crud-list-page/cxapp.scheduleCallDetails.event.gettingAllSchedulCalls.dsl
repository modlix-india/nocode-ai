FUNCTION gettingAllSchedulCalls
    LOGIC
        if: System.If(condition = Page.number = undefined)
            true
                setStore1: UIEngine.SetStore(path = "Page.number", value = 0) AFTER Steps.if.true
            output
                if1: System.If(condition = Page.size = undefined) AFTER Steps.if.output
                    true
                        setStore2: UIEngine.SetStore(path = "Page.size", value = 5) AFTER Steps.if1.true
                    output
                        if2: System.If(condition = Page.save) AFTER Steps.if1.output
                            false
                                if3: System.If(condition =  Page.serchUserName  = undefined) AFTER Steps.if2.false
                                    true
                                        setStore3: UIEngine.SetStore(path = "Page.filterObject", value = {
    "operator": "AND",
    "conditions": [
        {
            "field": "projectId",
            "operator": "IN",
            "multiValue": []
        },
        {
            "field": "callType",
            "operator": "IN",
            "multiValue": [
                "Telephonic call"
            ]
        }
    ]
}) AFTER Steps.if3.true
                            output
                                getAllCallDetails: hrms.getAllCallDetails(pageNumber = Page.number, size = Page.size??5, filterObject = Page.filterObject) AFTER Steps.if2.output
                                    output
                                        setStore: UIEngine.SetStore(path = "Page.AllScheduleCalls", value = Steps.getAllCallDetails.output.scheduleCallDetails)
                                            output
                                                setStore4: UIEngine.SetStore(path = "Page.lastPageNumber", value = {{(Page.AllScheduleCalls.total??0)%(Page.size??10) = 0}} ? {{(Page.AllScheduleCalls.total??0)//(Page.size??10)-1}} : {{(Page.AllScheduleCalls.total??0)//(Page.size??10)}}) AFTER Steps.setStore.output
                                                    output
                                                        setStore5: UIEngine.SetStore(path = "Page.isSameFirstLastPage", value = {{Page.lastPageNumber}} = {{Page.number}}) AFTER Steps.setStore4.output