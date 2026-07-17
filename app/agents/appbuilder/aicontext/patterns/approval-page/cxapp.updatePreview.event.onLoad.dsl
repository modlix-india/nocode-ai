FUNCTION onLoad
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.pro", value = {})
            output
                setStore2: UIEngine.SetStore(path = "Page.id", value = 1) AFTER Steps.setStore1.output
                    output
                        setStore3: UIEngine.SetStore(path = `'Page.pro.{{Page.id}}'`, value = {}) AFTER Steps.setStore2.output
                            output
                                read: CoreServices.Storage.Read(storageName = "ProjectUpdates", appCode = "rim", dataObjectId = Url.pathParts[1]) AFTER Steps.setStore3.output
                                    output
                                        setStore: UIEngine.SetStore(path = "Page.previewData", value = Steps.read.output.result)