FUNCTION loadData
    LOGIC
        setInitData: UIEngine.SetStore(path = "Page.appsSearch", value = {
    "condition": {
        "field": "appName",
        "operator": "STRING_LOOSE_EQUAL",
        "value": ""
    }
})
            output
                sendData: UIEngine.SendData(url = "api/security/applications/query", method = "POST", payload = Page.appsSearch) AFTER Steps.setInitData.output
                    error
                        message: UIEngine.Message(msg = Steps.sendData.error.data)
                    output
                        setStore: UIEngine.SetStore(path = "Page.searchedApps", value = Steps.sendData.output.data)
                            output
                                if_Copy_1: System.If(condition = (Page.searchedApps.content.length ?? 0 ) > 0) AFTER Steps.setStore.output
                                    true
                                        setStore_Copy_1: UIEngine.SetStore(path = "Page.selectedAppCode", value = Page.searchedApps.content[0].appCode) AFTER Steps.if_Copy_1.true
                                            output
                                                selectedDropDown: _.selectedDropDown() AFTER Steps.setStore_Copy_1.output