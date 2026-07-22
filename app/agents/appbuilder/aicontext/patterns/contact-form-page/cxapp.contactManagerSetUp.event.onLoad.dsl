FUNCTION onLoad
    LOGIC
        setFilter: UIEngine.SetStore(path = "Page.query", value = {
    "operator": "AND",
    "conditions": []
})
            output
                updateFilterWithName: UIEngine.SetStore(path = "Page.projectName", value = {
    "field": "projectFullName",
    "operator": "EQUALS"
}) AFTER Steps.setFilter.output
                    output
                        setProjectName: UIEngine.SetStore(path = "Page.projectName.value", value = Url.pathParts[1]) AFTER Steps.updateFilterWithName.output
                            output
                                insertLast: System.Array.InsertLast(source = Page.query.conditions, element = Page.projectName) AFTER Steps.setProjectName.output
                                    output
                                        setStore: UIEngine.SetStore(path = "Page.query.conditions", value = Steps.insertLast.output.result)
                                            output
                                                setClientId: UIEngine.SetStore(path = "Page.clientId", value = {
    "field": "createdClientId",
    "operator": "EQUALS"
}) AFTER Steps.setStore.output
                                                    output
                                                        if: System.If(condition = Store.clientType.isClient) AFTER Steps.setClientId.output
                                                            true
                                                                setIfClient: UIEngine.SetStore(path = "Page.clientId.value", value = {{Store.auth.user.clientId}}) AFTER Steps.if.true
                                                            false
                                                                ifCustomer: System.If(condition = Store.clientType.isCustomer) AFTER Steps.if.false
                                                                    true
                                                                        setIfCustomer: UIEngine.SetStore(path = "Page.clientId.value", value = {{Store.auth.loggedInClientId}}) AFTER Steps.ifCustomer.true
                                                            output
                                                                insertLast1: System.Array.InsertLast(source = Page.query.conditions, element = Page.clientId) AFTER Steps.if.output
                                                                    output
                                                                        setStore1: UIEngine.SetStore(value = Steps.insertLast1.output.result, path = "Page.query.conditions")
                                                                            output
                                                                                readPage: CoreServices.Storage.ReadPage(appCode = "rim", storageName = "Project", filter = Page.query, count = false, size = 1) AFTER Steps.setStore1.output
                                                                                    error
                                                                                        message: UIEngine.Message(msg = Steps.readPage.error.result)
                                                                                    output
                                                                                        setStore2: UIEngine.SetStore(path = "Page.projectDetails", value = Steps.readPage.output.result.content[0])