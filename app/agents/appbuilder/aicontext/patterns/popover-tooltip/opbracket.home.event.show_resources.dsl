FUNCTION show_resources
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.showResources", value = `(Page.showResources??'') = 'show' ? 'close' : 'show'`)