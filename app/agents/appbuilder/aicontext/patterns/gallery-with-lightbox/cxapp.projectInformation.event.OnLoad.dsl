FUNCTION OnLoad
    LOGIC
        setStore5: UIEngine.SetStore(path = "Page.count", value = 0)
        read: CoreServices.Storage.Read(dataObjectId = Store.urlDetails.pathParts[1], storageName = "Project", appCode = "rim")
            output
                setStore14: UIEngine.SetStore(path = "Page.project", value = Steps.read.output.result)