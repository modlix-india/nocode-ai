FUNCTION openGrid2
    LOGIC
        setStore_Copy_1: UIEngine.SetStore(path = "Page.toggleGrid1", value = false)
        setStore_Copy_2: UIEngine.SetStore(path = "Page.toggleGrid3", value = false)
        setStore_Copy_2_Copy_1: UIEngine.SetStore(path = "Page.toggleGrid4", value = false)
        setStore_Copy_2_Copy_2: UIEngine.SetStore(path = "Page.toggleGrid5", value = false)
        setStore_Copy_2_Copy_3: UIEngine.SetStore(path = "Page.toggleGrid6", value = false)
        setStore: UIEngine.SetStore(path = "Page.toggleGrid2", value = not Page.toggleGrid2)