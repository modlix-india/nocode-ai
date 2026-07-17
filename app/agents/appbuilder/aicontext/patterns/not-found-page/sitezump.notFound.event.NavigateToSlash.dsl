FUNCTION NavigateToSlash
    LOGIC
        setStore: UIEngine.SetStore(path = "Store.currentApp", value = `undefined`)
            output
                navigate: UIEngine.Navigate(linkPath = "/") AFTER Steps.setStore.output