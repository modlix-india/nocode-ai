FUNCTION show_products
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.showProducts", value = `(Page.showProducts??'') = 'show' ? 'close' : 'show'`)