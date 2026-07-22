FUNCTION onClickSubmitOTP
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.OTP", value = `false`)
            output
                setStore1: UIEngine.SetStore(path = "Page.PIN", value = `true`) AFTER Steps.setStore.output