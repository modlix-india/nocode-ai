FUNCTION SaveBusinessDetails
    LOGIC
        if1: System.If(condition = Page.details.content[0]._id)
            true
                update1: CoreServices.Storage.Update(dataObjectId = Page.details.content[0]._id, storageName = "BusinessDetails", appCode = "kyc", dataObject = Page.details.content[0]) AFTER Steps.if1.true
        if2: System.If(condition = Store.clientType.isClient)
            true
                setStore1: UIEngine.SetStore(path = "Page.clientOnboarding.defaultEmailId", value = Store.auth.user.emailId) AFTER Steps.if2.true
            output
                if: System.If(condition = Page.clientOnboarding.applicationLogo) AFTER Steps.if2.output
                    true
                        update: CoreServices.Storage.Update(appCode = "cxapp", dataObjectId = Page.clientOnboarding._id, storageName = "BusinessDetails", dataObject = Page.clientOnboarding, isPartial = true) AFTER Steps.if.true
                            error
                                message: UIEngine.Message(msg = "Mandatory fields are missing. Please fill all the fields.") AFTER Steps.update.error.result
                            output
                                setStore: UIEngine.SetStore(path = "Page.updatedDetails", value = Steps.update.output.result)
                                    output
                                        navigate: UIEngine.Navigate(linkPath = "/clientConfiguration") AFTER Steps.setStore.output
                                            output
                                                refresh_Copy_1: UIEngine.Refresh() AFTER Steps.navigate.output
                                message1: UIEngine.Message(msg = "Updated Details Successfully", type = "SUCCESS") AFTER Steps.update.output.result
                                    output
                                        refresh: UIEngine.Refresh() AFTER Steps.message1.output
                    false
                        cxappCreate: CoreServices.Storage.Create(dataObject = Page.clientOnboarding, storageName = "BusinessDetails", appCode = "cxapp") AFTER Steps.if.false