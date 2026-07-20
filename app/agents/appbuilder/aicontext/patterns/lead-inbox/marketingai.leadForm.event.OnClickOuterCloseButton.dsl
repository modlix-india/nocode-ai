FUNCTION OnClickOuterCloseButton
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.form", value = not Page.form)
        setStore1: UIEngine.SetStore(path = "Page.newForm", value = false)