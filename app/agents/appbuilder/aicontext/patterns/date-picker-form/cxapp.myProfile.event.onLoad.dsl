FUNCTION onLoad
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.showPersonalInfo", value = true)
        setStore4: UIEngine.SetStore(path = "Page.showProjects", value = true)
        setStore_Copy_1_Copy_1: UIEngine.SetStore(path = "Store.clientType.isClient", value = Store.auth.loggedInClientCode = Store.auth.client.code)
        setStore_Copy_1: UIEngine.SetStore(path = "Store.clientType.isCustomer", value = Store.auth.loggedInClientCode != Store.auth.client.code)
        setStore4_Copy_1: UIEngine.SetStore(path = "Page.showKycAccounts", value = "accounts")
        if3: System.If(condition = Url.pathParts[2]  and  Url.pathParts[3] )
            true
                getBookingDetailsByBookingId: _.getBookingDetailsByBookingId() AFTER Steps.if3.true
        onclickProfile: _.onclickProfile()
        new_Function_2: _.New_Function_2()
            output
                if2: System.If(condition = `Url.pathParts[1] = "documents" and Url.pathParts[2] = undefined  `) AFTER Steps.new_Function_2.output
                    true
                        getBookingDetailsOpt: _.getBookingDetailsOpt() AFTER Steps.if2.true
                    output
                        getCurrentUserKYCs: kyc.getCurrentUserKYCs() AFTER Steps.if2.output
                            output
                                setStore: UIEngine.SetStore(path = "Page.kycUsers", value = Steps.getCurrentUserKYCs.output.kycDetails)
                                    output
                                        if: System.If(condition = Page.kycUsers.length > 0) AFTER Steps.setStore.output
                                            false
                                                setStore11: UIEngine.SetStore(path = "Page.showText", value = "show") AFTER Steps.if.false
                        setStore1_Copy_2: UIEngine.SetStore(path = "Page.showSpinner", value = true) AFTER Steps.if2.output
                            output
                                getCurrentUserProfile: hrms.getCurrentUserProfile() AFTER Steps.setStore1_Copy_2.output
                                    output
                                        if1: System.If(condition = Steps.getCurrentUserProfile.output.userProfile.length > 1)
                                            true
                                                setStore8: UIEngine.SetStore(path = "Page.userDetails", value = Steps.getCurrentUserProfile.output.userProfile.content[0]) AFTER Steps.if1.true
                                                    output
                                                        setStore10: UIEngine.SetStore(path = "Page.userDetailsCopy", value = Page.userDetails) AFTER Steps.setStore8.output
                                                        if2_Copy_1: System.If(condition = Page.userDetails.fullName) AFTER Steps.setStore8.output
                                                            true
                                                                setStore22222_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.fullName", value = Page.userDetails.fullName) AFTER Steps.setStore8.output, Steps.if2_Copy_1.true
                                                            false
                                                                setStore22222_Copy_1: UIEngine.SetStore(path = "Page.fullName", value = `(Page.userDetails.firstName) + ' ' + (Page.userDetails.lastName)`) AFTER Steps.setStore8.output, Steps.if2_Copy_1.false
                                                            output
                                                                setStore1_Copy_2_Copy_1: UIEngine.SetStore(path = "Page.showSpinner", value = false) AFTER Steps.if2_Copy_1.output
                                            false
                                                setStore2_Copy_2_Copy_1: UIEngine.SetStore(path = "Page.userDetails.lastName", value = Store.auth.user.lastName) AFTER Steps.if1.false
                                                setStore2_Copy_2: UIEngine.SetStore(path = "Page.userDetails.firstName", value = Store.auth.user.firstName) AFTER Steps.if1.false
                                                    output
                                                        setStore22222: UIEngine.SetStore(path = "Page.fullName", value = `(Page.userDetails.firstName) + ' ' + (Page.userDetails.lastName)`) AFTER Steps.setStore2_Copy_2.output, Steps.setStore2_Copy_2_Copy_1.output
                                                            output
                                                                setStore2_Copy_1: UIEngine.SetStore(path = "Page.userDetails.emailId", value = Store.auth.user.emailId) AFTER Steps.setStore22222.output
                                                                    output
                                                                        setStore2_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.userDetails.phoneNumber", value = Store.auth.user.phoneNumber) AFTER Steps.setStore2_Copy_1.output
                                                                            output
                                                                                setStore10_Copy_1: UIEngine.SetStore(path = "Page.userDetailsCopy", value = Page.userDetails) AFTER Steps.setStore2_Copy_1_Copy_1.output
                                                                                setStore1_Copy_2_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.showSpinner", value = false) AFTER Steps.setStore2_Copy_1_Copy_1.output
        setStore2: UIEngine.SetStore(path = "Page.changePasswordGrid ", value = false)
        setStore3: UIEngine.SetStore(path = "Page.showsavepassword", value = false)
        setStore20: UIEngine.SetStore(path = "Page.dateOfBirthGrid", value = false)