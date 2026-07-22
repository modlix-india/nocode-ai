FUNCTION onClick_save2
    LOGIC
        saveKYC: kyc.newSaveKYC(kyc = Page.form)
            error
                message: UIEngine.Message(msg = Steps.saveKYC.error.message)
            output
                if: System.If(condition = Steps.saveKYC.output.kyc)
                    true
                        setStore: UIEngine.SetStore(path = "Page.count", value = Page.count + 1) AFTER Steps.if.true
                setStore1: UIEngine.SetStore(path = "Page.form", value = Steps.saveKYC.output.kyc)