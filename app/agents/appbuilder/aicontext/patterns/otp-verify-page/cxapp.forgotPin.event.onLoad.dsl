FUNCTION onLoad
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.getOTP", value = `true`)
            output
                setStore1: UIEngine.SetStore(path = "Page.OTP", value = `false`) AFTER Steps.setStore.output
                    output
                        setStore2: UIEngine.SetStore(path = "Page.PIN", value = `false`) AFTER Steps.setStore1.output
                            output
                                setStore3: UIEngine.SetStore(path = "Page.count", value = `15`) AFTER Steps.setStore2.output
                                    output
                                        setStore4: UIEngine.SetStore(path = "Page.timer", value = `false`) AFTER Steps.setStore3.output
                        setStore2_Copy_1: UIEngine.SetStore(path = "Page.resendButton", value = false) AFTER Steps.setStore1.output
                setStore1_Copy_1: UIEngine.SetStore(path = "Page.secondsLeft", value = 15) AFTER Steps.setStore.output