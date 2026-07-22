FUNCTION NavigateToAccountHome
    LOGIC
        setStore: UIEngine.SetStore(path = "Store.currentApp", value = `undefined`)
            output
                navigate: UIEngine.Navigate(linkPath = "/accountHome") AFTER Steps.setStore.output