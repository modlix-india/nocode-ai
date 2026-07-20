FUNCTION getBpConfigurationDetails
    LOGIC
        getBpConfiguration: leadzump.getBpConfiguration(clientCode = Store.auth.client.code)
            output
                setStore: UIEngine.SetStore(path = "Page.bpConfiguration", value = Steps.getBpConfiguration.output.bpConfiguration.content[0])
                    output
                        setStore1: UIEngine.SetStore(path = "Page.appLogoDetails", value = Page.bpConfiguration.appLogo) AFTER Steps.setStore.output
                        setStore1_Copy_1: UIEngine.SetStore(path = "Page.reLogoDetails", value = Page.bpConfiguration.reLogo) AFTER Steps.setStore.output