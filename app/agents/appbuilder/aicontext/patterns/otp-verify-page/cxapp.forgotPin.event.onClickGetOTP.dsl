FUNCTION onClickGetOTP
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.OTP", value = `true`)
            output
                setStore1: UIEngine.SetStore(path = "Page.getOTP", value = `false`) AFTER Steps.setStore.output
                    output
                        setStore2: UIEngine.SetStore(path = "Page.timer", value = `true`) AFTER Steps.setStore1.output