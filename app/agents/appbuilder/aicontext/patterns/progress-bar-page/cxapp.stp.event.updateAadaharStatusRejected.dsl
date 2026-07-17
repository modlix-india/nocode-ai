FUNCTION updateAadaharStatusRejected
    LOGIC
        updateDocStatus: kyc.updateDocStatus(kyc = Page.kycDetails, document = `"aadhar"`, status = `false`)
            output
                getSingleKYC: kyc.getSingleKYC(kycId = Page.kycDetails._id) AFTER Steps.updateDocStatus.output
                    output
                        setStore: UIEngine.SetStore(path = "Page.kycDetails", value = Steps.getSingleKYC.output.kycDetails)
                            output
                                onClickAadharPopup: _.onClickAadharPopup() AFTER Steps.setStore.output