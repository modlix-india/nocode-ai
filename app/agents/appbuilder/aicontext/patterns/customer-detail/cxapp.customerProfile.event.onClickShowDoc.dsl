FUNCTION onClickShowDoc
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.projectId", value = Parent.projectId)
        setStore1: UIEngine.SetStore(path = "Page.showDoc", value = true)