FUNCTION read
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.filter", value = {
    "operator": "AND",
    "conditions": [
        {
            "field": "projectId",
            "value": ""
        }
    ]
})
            output
                setStore2_Copy_1: UIEngine.SetStore(path = "Page.filter.conditions[0].value", value = Page.projectDetails._id) AFTER Steps.setStore1.output
                    output
                        readPage: CoreServices.Storage.ReadPage(storageName = "cashFreeOwnerDetails", appCode = "cxapp", filter = Page.filter) AFTER Steps.setStore2_Copy_1.output
                            output
                                if: System.If(condition = Steps.readPage.output.result.content[0] != undefined) AFTER Steps.readPage.output
                                    true
                                        setStore: UIEngine.SetStore(path = "Page.ownerCashfree", value = Steps.readPage.output.result.content[0]) AFTER Steps.if.true