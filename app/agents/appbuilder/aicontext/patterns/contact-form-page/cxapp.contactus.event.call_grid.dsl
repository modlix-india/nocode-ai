FUNCTION call_grid
    LOGIC
        setStore1: UIEngine.SetStore(path = `'Page.isOpen'`, value = true)
        setStore: UIEngine.SetStore(path = `'Page.currentid'`, value = Parent._id)