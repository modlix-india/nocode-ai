FUNCTION onClick_save1
    LOGIC
        saveKYC: kyc.newSaveKYC(kyc = Page.form)
            error
                message: UIEngine.Message(msg = Steps.saveKYC.error.message)
            output
                setStore1: UIEngine.SetStore(path = "Page.form", value = Steps.saveKYC.output.kyc)
                if: System.If(condition = Steps.saveKYC.output.kyc)
                    true
                        setStore: UIEngine.SetStore(path = "Page.count", value = Page.count + 1) AFTER Steps.if.true