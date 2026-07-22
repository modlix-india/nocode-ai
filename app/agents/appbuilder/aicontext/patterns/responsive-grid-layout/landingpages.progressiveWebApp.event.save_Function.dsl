FUNCTION save_Function
    LOGIC
        setStore: UIEngine.SetStore(path = `'Page.appdef.properties.manifest'`, value = Page.manifest)
            output
                sendData: UIEngine.SendData(url = `'api/ui/applications/{{Page.appdef.id}}'`, method = "PUT", payload = Page.appdef) AFTER Steps.setStore.output
                    error
                        message: UIEngine.Message(msg = Steps.sendData.error.data)
                    output
                        message_Copy_1: UIEngine.Message(msg = "Changes auto saved", type = "SUCCESS") AFTER Steps.sendData.output.data
                            output
                                onload: _.onload() AFTER Steps.message_Copy_1.output