FUNCTION validation_trigger
    LOGIC
        setStore: UIEngine.SetStore(path = "Store.validations.scheduleAcallConfiguration", value = {})
            output
                setStore1: UIEngine.SetStore(path = "Store.validationTriggers.scheduleAcallConfiguration", value = {}) AFTER Steps.setStore.output