FUNCTION onClickSelectField
    LOGIC
        activeIndex: UIEngine.SetStore(path = "Page.activeIndex", value = Parent.__index)
        setStore: UIEngine.SetStore(path = "Page.activeField.name", value = Parent.name)
        addingPhoneNumber: UIEngine.SetStore(path = "Page.activeField.phoneNumber", value = "undefined")