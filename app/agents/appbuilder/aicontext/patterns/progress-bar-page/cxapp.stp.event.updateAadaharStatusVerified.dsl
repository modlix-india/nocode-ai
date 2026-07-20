FUNCTION updateAadaharStatusVerified
    LOGIC
        updateDocStatus: kyc.updateDocStatus(kyc = Page.kycDetails, document = `"aadhar"`, status = `true`)
            output
                getSingleKYC: kyc.getSingleKYC(kycId = Page.kycDetails._id) AFTER Steps.updateDocStatus.output
                    output
                        setStore: UIEngine.SetStore(path = "Page.kycDetails", value = Steps.getSingleKYC.output.kycDetails)
                            output
                                onClickAadharPopup: _.onClickAadharPopup() AFTER Steps.setStore.output