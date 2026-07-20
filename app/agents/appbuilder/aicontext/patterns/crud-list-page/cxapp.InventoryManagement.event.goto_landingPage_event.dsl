FUNCTION goto_landingPage_event
    LOGIC
        navigate: UIEngine.Navigate(linkPath = `'/landingPage/{{Url.pathParts[1]}}'`)
        setStore: UIEngine.SetStore(path = "Store.validations.InventoryManagement", value = {})
            output
                setStore1: UIEngine.SetStore(path = "Store.validationTriggers.InventoryManagement", value = {}) AFTER Steps.setStore.output