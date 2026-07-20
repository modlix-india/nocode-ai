FUNCTION Save
    LOGIC
        sendData: UIEngine.SendData(url = `'/api/ui/applications/{{Page.app.id}}'`, method = "PUT", payload = Page.app) /* Save the application definintion */
            error
                message: UIEngine.Message(msg = Steps.sendData.error.data) /* If there is any error show a message */
            output
                if: System.If(condition = Steps.sendData.output.data) /* If it is saved properly */
                    true
                        setStore: UIEngine.SetStore(path = "Page.app", value = Steps.sendData.output.data) AFTER Steps.if.true /* Save to Page.app */
                        message1: UIEngine.Message(msg = "Saved", type = "SUCCESS") AFTER Steps.if.true /* Saved message */
                        setStore1: UIEngine.SetStore(path = `'Store.appDefs.{{Page.app.appCode}}'`, value = Steps.sendData.output.data) AFTER Steps.if.true /* Save to app defs also for further use */