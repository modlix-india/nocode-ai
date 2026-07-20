FUNCTION appsDropdown
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.appsSearch.condition.value", value = Page.appsSearchConditionValue)
            output
                sendData: UIEngine.SendData(url = "api/security/applications/query", method = "POST", payload = Page.appsSearch) AFTER Steps.setStore1.output
                    error
                        message: UIEngine.Message(msg = Steps.sendData.error.data)
                    output
                        setStore: UIEngine.SetStore(path = "", value = Steps.sendData.output.data)