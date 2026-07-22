FUNCTION delete_page
    LOGIC
        deletePageStorage: UIEngine.DeleteData(url = `'api/core/data/PageStorage/{{Parent._id}}'`)
            error
                message_Copy_1: UIEngine.Message(msg = "Delete Failed") AFTER Steps.deletePageStorage.error.data
            output
                if: System.If(condition = Steps.deletePageStorage.output.data and Steps.deleteActualPage.output.data)
                    true
                        successmessage: UIEngine.Message(msg = "Page Delete Successful", type = "SUCCESS") AFTER Steps.if.true
                        loadPages: _.loadPages() AFTER Steps.if.true
        deleteActualPage: UIEngine.DeleteData(url = `'api/ui/pages/{{Parent.page.id}}'`)
            error
                message: UIEngine.Message(msg = "Delete Failed or partially deleted") AFTER Steps.deleteActualPage.error