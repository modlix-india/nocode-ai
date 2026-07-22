FUNCTION loadPages
    LOGIC
        if: System.If(condition = Store.urlDetails.pathParts[1] != null)
            true
                setStore1: UIEngine.SetStore(path = "Page.pageQuery.size", value = Page.pages.size ?? 10) AFTER Steps.if.true
                setStore2: UIEngine.SetStore(path = "Page.pageQuery.page", value = Page.pages.number ?? 0) AFTER Steps.if.true
                setStore: UIEngine.SetStore(path = "Page.pageQuery", value = {
    "excludeFields": true,
    "fields": [
        "page.componentDefinition"
    ],
    "condition": {
        "field": "appCode"
    }
}) AFTER Steps.if.true
                    output
                        if1: System.If(condition = Page.pageSearchQuery != null) AFTER Steps.setStore.output, Steps.setStore1.output, Steps.setStore2.output
                            true
                                setStore3: UIEngine.SetStore(path = "Page.pageQuery.condition", value = {
    "conditions": [
        {
            "field": "page.name",
            "operator": "STRING_LOOSE_EQUAL"
        },
        {
            "field": "page.appCode"
        }
    ],
    "operator": "AND"
}) AFTER Steps.if1.true
                                    output
                                        setStore4: UIEngine.SetStore(path = "Page.pageQuery.condition.conditions[0].value", value = Page.pageSearchQuery) AFTER Steps.setStore3.output
                                            output
                                                setStore7: UIEngine.SetStore(path = "Page.pageQuery.condition.conditions[1].value", value = Store.urlDetails.pathParts[1]) AFTER Steps.setStore4.output
                            false
                                setStore5: UIEngine.SetStore(path = "Page.pageQuery.condition", value = {
    "field": "page.appCode"
}) AFTER Steps.if1.false
                                    output
                                        setStore6: UIEngine.SetStore(path = "Page.pageQuery.condition.value", value = Store.urlDetails.pathParts[1]) AFTER Steps.setStore5.output
                            output
                                fetch: UIEngine.SendData(url = "api/core/data/PageStorage/query", queryParams = null, method = "POST", payload = Page.pageQuery) AFTER Steps.if1.output
                                    error
                                        messageFetchStep: UIEngine.Message(msg = Steps.fetch.error.data)
                                    output
                                        store: UIEngine.SetStore(path = "Page.pages", value = Steps.fetch.output.data)