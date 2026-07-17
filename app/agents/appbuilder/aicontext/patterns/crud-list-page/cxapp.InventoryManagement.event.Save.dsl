FUNCTION Save
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.project.inventoryManagement.ownershipDetails", value = Page.allOwnershipDetails)
            output
                update: CoreServices.Storage.Update(dataObjectId = Page.project._id, storageName = "Project", appCode = "rim", dataObject = Page.project) AFTER Steps.setStore.output
                    error
                        message: UIEngine.Message(msg = Steps.update.error.result)
                    output
                        message_Copy_1: UIEngine.Message(msg = "Inventory Management details Updated", type = "SUCCESS") AFTER Steps.update.output.result
                            output
                                navigateBack: UIEngine.NavigateBack() AFTER Steps.message_Copy_1.output