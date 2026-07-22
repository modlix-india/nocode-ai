FUNCTION on_toggleButton
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.id", value = Parent._id)
        setStore: UIEngine.SetStore(path = "Page.isViewDetailsGridOpen", value = not Page.isViewDetailsGridOpen)