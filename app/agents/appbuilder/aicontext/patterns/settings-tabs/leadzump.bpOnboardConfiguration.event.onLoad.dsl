FUNCTION onLoad
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.dummydata", value = [])
        setStore2: UIEngine.SetStore(path = "Page.activeTab", value = `"Verification"`)
        getBpConfiguration: leadzump.getBpConfiguration(clientCode = Store.auth.client.code)
            output
                setStore1: UIEngine.SetStore(value = Steps.getBpConfiguration.output.bpConfiguration.content[0], path = "Page.bpConfiguration")
                    output
                        checkConfigAndCreate: _.checkConfigAndCreate() AFTER Steps.setStore1.output
                            output
                                if: System.If(condition = Page.bpConfiguration.documents = undefined) AFTER Steps.checkConfigAndCreate.output
                                    true
                                        setStore3: UIEngine.SetStore(path = "Page.bpConfiguration.documents", value = []) AFTER Steps.if.true