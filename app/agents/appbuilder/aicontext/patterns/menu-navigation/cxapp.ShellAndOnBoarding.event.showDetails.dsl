FUNCTION showDetails
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.showDetails", value = true)
            output
                if: System.If(condition = `Page.clientOnboarding.title != "" and Page.clientOnboarding.description != "" and Page.clientOnboarding.image != "" and Page.clientOnboarding.applicationLogo != ""`) AFTER Steps.setStore1.output
                    true
                        setStore: UIEngine.SetStore(path = "Page.showDetails", value = false) AFTER Steps.if.true