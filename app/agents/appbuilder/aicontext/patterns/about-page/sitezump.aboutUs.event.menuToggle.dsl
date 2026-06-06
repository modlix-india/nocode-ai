FUNCTION menuToggle
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.menu", value = not Page.menu)