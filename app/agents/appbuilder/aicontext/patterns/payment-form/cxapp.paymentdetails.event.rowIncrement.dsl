FUNCTION rowIncrement
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.rows", value = {{Page.rows}} + 1 )