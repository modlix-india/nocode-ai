FUNCTION onClickOngoingPurchase
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.ongoingPurchase", value = not Page.ongoingPurchase)