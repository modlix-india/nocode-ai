FUNCTION Save
    LOGIC
        update: CoreServices.Storage.Update(dataObjectId = Store.urlDetails.pathParts[1], storageName = "Project", appCode = "rim", dataObject = Page.project)
            error
                message: UIEngine.Message(msg = Steps.update.error.result)
            output
                message_Copy_1: UIEngine.Message(msg = "Inventory Management details Updated", type = "SUCCESS") AFTER Steps.update.output.result
                    output
                        navigate: UIEngine.Navigate(linkPath = `'/configure/{{Store.urlDetails.pathParts[1]}}'`) AFTER Steps.message_Copy_1.output