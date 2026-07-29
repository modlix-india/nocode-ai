FUNCTION on_load
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.count", value = 0)
        setStore3: UIEngine.SetStore(path = "Page.showPopup", value = false)
        encodeUrl: UIEngine.EncodeURIComponent(uriComponent = Store.urlDetails.queryParameters.redirect)
            output
                setStore6: UIEngine.SetStore(path = "Page.encodedUrl", value = Steps.encodeUrl.output.encodedValue)
        setStore5: UIEngine.SetStore(path = "Page.flag", value = false)
            output
                setStore14: UIEngine.SetStore(path = "Page.stepperDataMobile", value = ["Personal", "Professional", "PAN"]) AFTER Steps.setStore5.output
                    output
                        setStore4: UIEngine.SetStore(path = "Page.stepperData", value = ["Personal Details", "Professional Details", "PAN & Bank details"]) AFTER Steps.setStore14.output
                            output
                                checkingIfModifyDetails: System.If(condition = `Store.urlDetails.pathParts[2] != "modifyDetails"`) AFTER Steps.setStore4.output
                                    true
                                        setStore8: UIEngine.SetStore(path = "Page.folderId", value = Store.urlDetails.pathParts[2]) AFTER Steps.checkingIfModifyDetails.true
                                    false
                                        getSingleKYC1: kyc.getSingleKYC(id = Store.urlDetails.pathParts[1], kycId = Store.urlDetails.pathParts[1]) AFTER Steps.checkingIfModifyDetails.false
                                            output
                                                setStore10: UIEngine.SetStore(path = "Page.form", value = Steps.getSingleKYC1.output.kycDetails)
                                                    output
                                                        setStore8_Copy_1: UIEngine.SetStore(path = "Page.folderId", value = Steps.getSingleKYC1.output.kycDetails.folderId) AFTER Steps.setStore10.output
                                    output
                                        if: System.If(condition = `Store.urlDetails.pathParts[1] = "folder" or Store.urlDetails.pathParts[1] = null or Store.urlDetails.pathParts[1] = undefined `) AFTER Steps.checkingIfModifyDetails.output
                                            false
                                                getSingleKYC: kyc.getSingleKYC(id = Store.urlDetails.pathParts[1], kycId = Store.urlDetails.pathParts[1]) AFTER Steps.if.false
                                                    output
                                                        setStore: UIEngine.SetStore(path = "Page.form", value = Steps.getSingleKYC.output.kycDetails)
                                                            output
                                                                previousRelationType: UIEngine.SetStore(value = Page.form.relationType, path = "Page.previousRelationType") AFTER Steps.setStore.output
                                                                    output
                                                                        if1: System.If(condition = Store.urlDetails.pathParts[2] ) AFTER Steps.previousRelationType.output
                                                                            true
                                                                                setStore7: UIEngine.SetStore(path = "Page.form.folderId", value = Page.folderId) AFTER Steps.if1.true
                                                                                    output
                                                                                        if2: System.If(condition = Page.form.individual.addressDetails.addressProofCopy) AFTER Steps.setStore7.output
                                                                                            true
                                                                                                setStore9: UIEngine.SetStore(path = "Page.addressDoc.file", value = Page.form.individual.addressDetails.addressProofCopy) AFTER Steps.if2.true
                                            output
                                                seperating_individualKycs: _.seperating_individualKycs() AFTER Steps.if.output
                                                    output
                                                        residential_status_onLoad: _.residential_status_onLoad() AFTER Steps.seperating_individualKycs.output
                                                            output
                                                                onClickProfession: _.onClickProfession() AFTER Steps.residential_status_onLoad.output
                                                                    output
                                                                        userData: _.userData() AFTER Steps.onClickProfession.output
                                                        if4: System.If(condition = Page.selfKyc.length = 0) AFTER Steps.seperating_individualKycs.output
                                                            true
                                                                selfRelationType: _.selfRelationType() AFTER Steps.if4.true
                                                            false
                                                                if3: System.If(condition = `Page.selfKyc[0].status = "PENDING"`) AFTER Steps.if4.false
                                                                    true
                                                                        selfRelationType1: _.selfRelationType() AFTER Steps.if3.true
                                                                    false
                                                                        getting_remaining_relations: _.getting_remaining_relations() AFTER Steps.if3.false
        setStore12: UIEngine.SetStore(path = "Page.profession", value = false)
            output
                setStore13: UIEngine.SetStore(path = "Page.testing", value = not Page.form.individual.basic.professionalInformation.designation  ? true : Page.form.individual.basic.professionalInformation.designation and Page.form.individual.basic.professionalInformation.companyName and Page.form.individual.basic.professionalInformation.industry and Page.form.individual.basic.professionalInformation.function and Page.form.individual.basic.professionalInformation.annualIncome and Page.form.individual.basic.professionalInformation.officeAddress.Line1 and Page.form.individual.basic.professionalInformation.officeAddress.locality and Page.form.individual.basic.professionalInformation.officeAddress.pincode and Page.form.individual.basic.professionalInformation.officeAddress.state and Page.form.individual.basic.professionalInformation.officeAddress.city) AFTER Steps.setStore12.output
        setStore_Copy_1: UIEngine.SetStore(path = "Page.searchedLocationData", value = [])
        readPage: CoreServices.Storage.ReadPage(storageName = "BusinessDetails", appCode = "kyc")
            output
                setStore2: UIEngine.SetStore(path = "Page.clientOnboarding", value = Steps.readPage.output.result)