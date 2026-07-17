FUNCTION onClick_PopupActivateButton2
    LOGIC
        getSingleKYC: kyc.getSingleKYC(id = Parent._id)
            output
                setStore: UIEngine.SetStore(path = "Page.kycDetails", value = Steps.getSingleKYC.output.kycDetails) AFTER Steps.getSingleKYC.output
                    output
                        setStore1: UIEngine.SetStore(path = `'Page.kycDetails.status'`, value = `'VERIFIED'`) AFTER Steps.setStore.output
                            output
                                newSaveKYC: kyc.newSaveKYC(kyc = Page.kycDetails) AFTER Steps.setStore1.output
                                    output
                                        refresh: UIEngine.Refresh() AFTER Steps.newSaveKYC.output.kyc
                                print: System.Print(values = Page.kycDetails) AFTER Steps.setStore1.output