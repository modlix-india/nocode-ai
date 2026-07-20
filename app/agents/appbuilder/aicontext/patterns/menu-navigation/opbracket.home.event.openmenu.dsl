FUNCTION openmenu
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.showMenu", value = `(Page.showMenu??'') = 'show' ? 'close' : 'show'`)