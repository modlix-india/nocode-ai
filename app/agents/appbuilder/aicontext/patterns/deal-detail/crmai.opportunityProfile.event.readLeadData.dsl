FUNCTION readLeadData
    LOGIC
        read1: CoreServices.Storage.Read(dataObjectId = Store.urlDetails.pathParts[1], storageName = "Opportunities")
            output
                setStore1: UIEngine.SetStore(path = "Page.activeOppDetails", value = Steps.read1.output.result)
                    output
                        read: CoreServices.Storage.Read(storageName = "Leads", dataObjectId = Page.activeOppDetails.leadId) AFTER Steps.setStore1.output
                            output
                                setStore: UIEngine.SetStore(path = "Page.activeLeadDetails", value = Steps.read.output.result)