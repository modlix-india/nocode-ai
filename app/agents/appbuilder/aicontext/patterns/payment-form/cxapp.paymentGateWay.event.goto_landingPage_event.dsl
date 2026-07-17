FUNCTION goto_landingPage_event
    LOGIC
        setStore: UIEngine.SetStore(path = "Store.validations.scheduleAcallConfiguration", value = {})
            output
                setStore1: UIEngine.SetStore(path = "Store.validationTriggers.scheduleAcallConfiguration", value = {}) AFTER Steps.setStore.output
                    output
                        navigate: UIEngine.Navigate(linkPath = `'/landingPage/{{Url.pathParts[1]}}'`) AFTER Steps.setStore1.output