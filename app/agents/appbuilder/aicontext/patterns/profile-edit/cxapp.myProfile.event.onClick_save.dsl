FUNCTION onClick_save
    LOGIC
        if1: System.If(condition = Store.auth.user.firstName )
            true
                setStore3: UIEngine.SetStore(path = "Page.fullName", deleteKey = true, value = `(Page.userDetails.firstName??'') +' ' + (Page.userDetails.lastName??'')`) AFTER Steps.if1.true
                    output
                        setStore3_Copy_2: UIEngine.SetStore(path = "Page.userDetails.firstName", deleteKey = true, value = `Page.userDetails.firstName??''`) AFTER Steps.setStore3.output
                            output
                                setStore8: UIEngine.SetStore(value = `Page.userDetails.lastName??''`, path = `'Page.userDetails.lastName'`) AFTER Steps.setStore3_Copy_2.output
                                    output
                                        setStore10: UIEngine.SetStore(path = `'Page.userDetails.bio'`, value = `Page.userDetails.bio??''`) AFTER Steps.setStore8.output
                setStore3_Copy_1: UIEngine.SetStore(path = "Page.fullName", deleteKey = true, value = `(Page.userDetails.firstName??'') +' ' + (Page.userDetails.lastName??'')`) AFTER Steps.if1.true
                    output
                        setStore3_Copy_2_Copy_1: UIEngine.SetStore(path = "Page.userDetails.firstName", deleteKey = true, value = `Page.userDetails.firstName??''`) AFTER Steps.setStore3_Copy_1.output
                            output
                                setStore8_Copy_1: UIEngine.SetStore(value = `Page.userDetails.lastName??''`, path = `'Page.userDetails.lastName'`) AFTER Steps.setStore3_Copy_2_Copy_1.output
                                    output
                                        setStore10_Copy_1: UIEngine.SetStore(path = `'Page.userDetails.bio'`, value = `Page.userDetails.bio??''`) AFTER Steps.setStore8_Copy_1.output
            output
                objectDeleteKey: System.Object.ObjectDeleteKey(source = Page.userDetails, key = "fullName") AFTER Steps.if1.output
                    output
                        setStore11: UIEngine.SetStore(path = "Page.userDetails", value = Steps.objectDeleteKey.output.value)
                            output
                                if2: System.If(condition = Page.userDetails.profilePicture) AFTER Steps.setStore11.output
                                    false
                                        setStore2: UIEngine.SetStore(path = "Page.userDetails.profilePicture", value = "") AFTER Steps.if2.false
                                    output
                                        if3: System.If(condition = Page.userDetails.firstName) AFTER Steps.if2.output
                                            false
                                                setStore5: UIEngine.SetStore(path = "Page.userDetails.firstName", value = "") AFTER Steps.if3.false
                                            output
                                                if3_Copy_1: System.If(condition = Page.userDetails.lastName) AFTER Steps.if3.output
                                                    false
                                                        setStore5_Copy_1: UIEngine.SetStore(path = "Page.userDetails.lastName", value = "") AFTER Steps.if3_Copy_1.false
                                                    output
                                                        if3_Copy_2: System.If(condition = Page.userDetails.bio) AFTER Steps.if3_Copy_1.output
                                                            false
                                                                setStore5_Copy_2: UIEngine.SetStore(path = "Page.userDetails.bio", value = "") AFTER Steps.if3_Copy_2.false
                                                            output
                                                                saveUserProfile: hrms.saveUserProfile(userDetails = Page.userDetails, clientCode = Store.auth.loggedInClientCode) AFTER Steps.if3_Copy_2.output
                                                                    error
                                                                        message: UIEngine.Message(msg = Steps.saveUserProfile.error.message)
                                                                    output
                                                                        if: System.If(condition = Steps.saveUserProfile.output.userDetails)
                                                                            true
                                                                                setStore: UIEngine.SetStore(path = "Page.showEditGrid", value = false) AFTER Steps.if.true
                                                                                setStore1: UIEngine.SetStore(path = `'Page.userProfileCopy'`, value = Page.userProfile) AFTER Steps.if.true
                                                                                message1: UIEngine.Message(msg = "Details Updated Successfully", type = "SUCCESS") AFTER Steps.if.true
                                                                                setStore4: UIEngine.SetStore(path = "Page.userDetailsCopy", value = Page.userDetails) AFTER Steps.if.true
                                                                        setStore9: UIEngine.SetStore(path = "Page.userDetails", value = Steps.saveUserProfile.output.userDetails)