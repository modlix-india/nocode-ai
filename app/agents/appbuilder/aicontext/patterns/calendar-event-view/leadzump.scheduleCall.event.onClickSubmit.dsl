FUNCTION onClickSubmit
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.show", value = "success")
            output
                userConfirmation: leadzump.userConfirmation(bookAcallDetails = Page.bookAcallDetails) AFTER Steps.setStore.output
                    output
                        create: CoreServices.Storage.Create(dataObject = Page.bookAcallDetails, appCode = "leadzump", storageName = "BookAcallDetails") AFTER Steps.userConfirmation.output