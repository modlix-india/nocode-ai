FUNCTION on_load
    LOGIC
        readPage: CoreServices.Storage.ReadPage(appCode = "cxapp", storageName = "BusinessDetails")
            error
                message: UIEngine.Message(msg = Steps.readPage.error.result)
            output
                setStore: UIEngine.SetStore(path = "Page.clientOnboarding", value = Steps.readPage.output.result.content[0])