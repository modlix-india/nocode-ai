FUNCTION onClickInventoryOpen
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.inventoryOpen", value = not Page.inventoryOpen)