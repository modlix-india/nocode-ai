FUNCTION onClick_Save1
    LOGIC
        newSaveKYC: kyc.newSaveKYC(kyc = Page.joint)
            error
                message: UIEngine.Message(msg = Steps.newSaveKYC.error.message)