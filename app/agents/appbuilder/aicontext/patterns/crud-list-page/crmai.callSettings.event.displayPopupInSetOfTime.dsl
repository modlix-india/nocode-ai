FUNCTION displayPopupInSetOfTime
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.showPage", value = "configurePhoneNumber")
        setStore_Copy_1: UIEngine.SetStore(path = "Page.hasData", value = false)
        objectData: UIEngine.SetStore(path = "Page.objectData.target", value = `"client"`)
        if: System.If(condition = Page.clientData.phoneNumber)
            true
                activeTab: UIEngine.SetStore(path = "Page.activeTab", value = "Configure number for pipeline") AFTER Steps.if.true
                    output
                        setStore_Copy_2: UIEngine.SetStore(path = "Page.activeData", value = "Configure number for pipeline") AFTER Steps.activeTab.output
                            output
                                readPiplines: _.readPiplines() AFTER Steps.setStore_Copy_2.output
            false
                setStore1: UIEngine.SetStore(path = "Page.activeTab", value = `"Default number"`) AFTER Steps.if.false