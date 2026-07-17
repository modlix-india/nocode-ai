FUNCTION onLoad
    LOGIC
        setStore_Copy_1: UIEngine.SetStore(path = "Store.clientType.isCustomer", value = Store.auth.loggedInClientCode != Store.auth.client.code)
        if1: System.If(condition = Store.clientType.isClient)
            true
                getUserdata: _.getUserdata() AFTER Steps.if1.true
        setS: UIEngine.SetStore(path = "Page.jointNames", value = {})
        setStore7: UIEngine.SetStore(path = "Page.keysArray", value = [])
        setStore8: UIEngine.SetStore(path = "Page.valuesArray", value = [])
        setStore9: UIEngine.SetStore(path = "Page.text", value = "Joint account")
        setStore_Copy_1_Copy_1: UIEngine.SetStore(path = "Store.clientType.isClient", value = Store.auth.loggedInClientCode = Store.auth.client.code)
            output
                if: System.If(condition = Store.clientType.isClient) AFTER Steps.setStore_Copy_1_Copy_1.output
                    false
                        if4: System.If(condition = `false`) AFTER Steps.if.false
                            true
                                setStore3: UIEngine.SetStore(path = "Page.kycId", value = Url.pathParts[1]) AFTER Steps.if4.true
                                    output
                                        if3: System.If(condition = Url.pathParts[1] != Store.auth.user.id) AFTER Steps.setStore3.output
                                            true
                                                getSingleKYC: kyc.getSingleKYC(kycId = Page.kycId) AFTER Steps.if3.true
                                                    output
                                                        setStore1: UIEngine.SetStore(path = "Page.kycDetails", value = Steps.getSingleKYC.output.kycDetails)
                                                            output
                                                                setStore4: UIEngine.SetStore(path = "Page.isAadhar", value = Page.kycDetails.individual.basic.personalInformation.aadharCard) AFTER Steps.setStore1.output
                                                                getProjectDetails: _.getProjectDetails() AFTER Steps.setStore1.output
                                                                setStore5: UIEngine.SetStore(path = "Page.isPan", value = Page.kycDetails.individual.panAndBank.panImage) AFTER Steps.setStore1.output
                                                                    output
                                                                        setStore6: UIEngine.SetStore(path = "Page.folderId", value = Page.kycDetails.folderId) AFTER Steps.setStore5.output
                        getCurrentProfileOnload: _.getCurrentProfileOnload() AFTER Steps.if.false
                    output
                        if6: System.If(condition = Store.clientType.isClient) AFTER Steps.if.output
                            true
                                setStore1_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.userId", value = {{Url.pathParts[1]}}) AFTER Steps.if6.true
                                    output
                                        getProfileByIdOnload: _.getProfileByIdOnload() AFTER Steps.setStore1_Copy_1_Copy_1.output
                            false
                                setStore10: UIEngine.SetStore(path = "Page.userId", value = Store.auth.user.id) AFTER Steps.if6.false
                            output
                                getKYCs: _.getKYCs() AFTER Steps.if6.output
                                    output
                                        selfAccount: _.selfAccount() AFTER Steps.getKYCs.output
                                        if2: System.If(condition = `Page.kycUsers[0].status = 'PENDING'`) AFTER Steps.getKYCs.output
                                            true
                                                if5: System.If(condition = Page.verifiedUsers.length = 0) AFTER Steps.if2.true
                                                    true
                                                        setStore8_Copy_1: UIEngine.SetStore(path = "Page.zeroKycs", value = `true`) AFTER Steps.if5.true
                                            false
                                                setStore2: UIEngine.SetStore(path = "Page.kycDetails", value = Page.kycUsers[0]) AFTER Steps.if2.false
                                                    output
                                                        setStore5_Copy_1: UIEngine.SetStore(path = "Page.isPan", value = Page.kycDetails.individual.panAndBank.panImage) AFTER Steps.setStore2.output
                                                        setStore4_Copy_1: UIEngine.SetStore(path = "Page.isAadhar", value = Page.kycDetails.individual.basic.personalInformation.aadharCard) AFTER Steps.setStore2.output
                                                        new_Function_3: _.New_Function_3() AFTER Steps.setStore2.output
                                                        setStore_Copy_1_Copy_2: UIEngine.SetStore(path = "Page.kycObject", value = Page.kycDetails) AFTER Steps.setStore2.output
                                getUserProfileById: hrms.getUserProfileById(userId = Page.userId, userCode = Store.auth.client.code) AFTER Steps.if6.output
                                    output
                                        setStore: UIEngine.SetStore(path = "Page.test", value = Steps.getUserProfileById.output.userProfile.content[0])