FUNCTION onClickBack
    LOGIC
        setStore_Copy_1: UIEngine.SetStore(path = "Page.index", value = {{Page.index ?? 0 }} - 1)