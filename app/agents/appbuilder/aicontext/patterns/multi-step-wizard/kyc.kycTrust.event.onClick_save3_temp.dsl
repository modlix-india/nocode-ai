FUNCTION onClick_save3_temp
    LOGIC
        tempSaveKyc: kyc.tempSaveKyc(kyc = Page.form)
            error
                message: UIEngine.Message(msg = Steps.tempSaveKyc.error.message)
            output
                if: System.If(condition = Steps.tempSaveKyc.output.kyc)
                    true
                        setStore: UIEngine.SetStore(path = "Page.count", value = Page.count + 1) AFTER Steps.if.true
                setStore1: UIEngine.SetStore(path = "Page.form", value = Steps.tempSaveKyc.output.kyc)