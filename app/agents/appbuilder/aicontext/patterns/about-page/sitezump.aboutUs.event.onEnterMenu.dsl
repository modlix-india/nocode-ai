FUNCTION onEnterMenu
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.onEnterMenu", value = not Page.onEnterMenu)