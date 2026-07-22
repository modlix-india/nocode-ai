FUNCTION toggleGrid
    LOGIC
        setStore_Copy_1: UIEngine.SetStore(path = "Page.toggleGrid2", value = false)
        setStore_Copy_2: UIEngine.SetStore(path = "Page.toggleGrid3", value = false)
        setStore_Copy_3: UIEngine.SetStore(path = "Page.toggleGrid4", value = false)
        setStore_Copy_4: UIEngine.SetStore(path = "Page.toggleGrid5", value = false)
        setStore_Copy_5: UIEngine.SetStore(path = "Page.toggleGrid6", value = false)
        setStore: UIEngine.SetStore(path = "Page.toggleGrid1", value = not Page.toggleGrid1)