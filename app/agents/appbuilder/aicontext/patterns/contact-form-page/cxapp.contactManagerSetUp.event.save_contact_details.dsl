FUNCTION save_contact_details
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.saveLoader", value = true)
            output
                update: CoreServices.Storage.Update(storageName = "Project", dataObjectId = Page.projectDetails._id, dataObject = Page.projectDetails, appCode = "rim") AFTER Steps.setStore.output
                    error
                        message: UIEngine.Message(msg = Steps.update.error.result)
                    output
                        navigateBack: UIEngine.NavigateBack() AFTER Steps.update.output
                            output
                                message1: UIEngine.Message(msg = "Contact manager details are updated", type = "SUCCESS") AFTER Steps.navigateBack.output
                                setStore_Copy_1: UIEngine.SetStore(path = "Page.saveLoader", value = false) AFTER Steps.navigateBack.output