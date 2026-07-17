FUNCTION checkConfigAndCreate
    LOGIC
        if: System.If(condition = Page.bpConfiguration.modeOfBpCreation = undefined)
            true
                setStore1: UIEngine.SetStore(path = "Page.initialBpConfig._id", value = Page.bpConfiguration._id) AFTER Steps.if.true
                    output
                        setStore1_Copy_1: UIEngine.SetStore(path = "Page.initialBpConfig.modeOfBpCreation", value = {
    "viaInvitation": true
}) AFTER Steps.setStore1.output
                            output
                                setStore1_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.initialBpConfig.businessPartnerVerification", value = {
    "phoneVerification": true,
    "setPassword": "<REDACTED>"
}) AFTER Steps.setStore1_Copy_1.output
                                    output
                                        setStore3: UIEngine.SetStore(path = "Page.initialBpConfig.loginOptions", value = {
    "viaPassword": "<REDACTED>",
    "viaOTP": true
}) AFTER Steps.setStore1_Copy_1_Copy_1.output
                                            output
                                                saveBpConfiguration: leadzump.saveBpConfiguration(bpConfiguration = Page.initialBpConfig) AFTER Steps.setStore3.output
                                                    output
                                                        setStore: UIEngine.SetStore(path = "Page.bpConfiguration", value = Steps.saveBpConfiguration.output.bpConfiguration)
                                                            output
                                                                setStore2: UIEngine.SetStore(path = "Page.initialBpConfig", deleteKey = true) AFTER Steps.setStore.output