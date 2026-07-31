FUNCTION onClickBack
    LOGIC
        setStore: UIEngine.SetStore(path = "Store.validations.InventoryManagement", value = {})
            output
                setStore1: UIEngine.SetStore(path = "Store.validationTriggers.InventoryManagement", value = {}) AFTER Steps.setStore.output
        navigate: UIEngine.Navigate(linkPath = `'/configure/{{Url.pathParts[1]}}/{{Url.pathParts[2]}}/{{Url.pathParts[3]}}/{{Url.pathParts[4]}}'`)