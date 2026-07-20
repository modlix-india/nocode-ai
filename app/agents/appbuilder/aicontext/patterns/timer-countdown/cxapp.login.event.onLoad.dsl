FUNCTION onLoad
    LOGIC
        setStore2: UIEngine.SetStore(path = "Page.identifierType", value = "EMAIL_ID")
            output
                readPage: CoreServices.Storage.ReadPage(storageName = "BusinessDetails") AFTER Steps.setStore2.output
                    error
                        message: UIEngine.Message(msg = Steps.readPage.error.result)
                    output
                        setStore: UIEngine.SetStore(path = "Page.businessDetails", value = Steps.readPage.output.result.content[0])