FUNCTION onClickAddTeammate
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.activePage", value = "INVITE")
            output
                setStore1: UIEngine.SetStore(path = "Page.teammateDetails", value = null, deleteKey = true) AFTER Steps.setStore.output
                    output
                        clearValidations: _.clearValidations() AFTER Steps.setStore1.output
        setStore2: UIEngine.SetStore(path = "Page.addTeammate", value = `true`)