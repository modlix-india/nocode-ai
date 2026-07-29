FUNCTION onClick_exploreMore
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.viewMoreProperties", value = not Page.viewMoreProperties)