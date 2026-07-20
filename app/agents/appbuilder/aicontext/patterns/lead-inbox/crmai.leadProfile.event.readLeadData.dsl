FUNCTION readLeadData
    LOGIC
        read: CoreServices.Storage.Read(storageName = "Leads", dataObjectId = Store.urlDetails.pathParts[1])
            output
                setStore: UIEngine.SetStore(path = "Page.activeLeadDetails", value = Steps.read.output.result)
                    output
                        getShortNameData: crmai.getShortName(string = Page.activeLeadDetails.fullName) AFTER Steps.setStore.output
                            output
                                shortName: UIEngine.SetStore(path = "Page.profileName", value = Steps.getShortNameData.output.shortName)
                        opportunityArrayForm: _.opportunityArrayForm() AFTER Steps.setStore.output
                            output
                                if: System.If(condition = Page.opportunityNames.length = 1 or Page.opportunityNames.length = 0) AFTER Steps.opportunityArrayForm.output
                                    true
                                        name: UIEngine.SetStore(path = "Page.opportunityNamesItem", value = Page.opportunityNames[0]) AFTER Steps.if.true
                                    false
                                        join: System.Array.Join(source = Page.opportunityNames, delimiter = " , ") AFTER Steps.if.false
                                            output
                                                opportunityNamesToString: UIEngine.SetStore(path = "Page.opportunityNamesItem", value = Steps.join.output.result)