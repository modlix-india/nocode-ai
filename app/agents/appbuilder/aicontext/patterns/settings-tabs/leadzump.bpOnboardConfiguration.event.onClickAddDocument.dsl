FUNCTION onClickAddDocument
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.addDocPopup", value = `true`)
        setStore2: UIEngine.SetStore(path = "Page.isEditDoc", value = `false`)
        setStore1: UIEngine.SetStore(path = "Page.singleDoc", deleteKey = true)