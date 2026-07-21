FUNCTION table_data_on_load
    LOGIC
        setStore3: UIEngine.SetStore(path = "Page.activeButton", value = "upcoming")
        setStore2: UIEngine.SetStore(path = `'Page.showPage'`, value = false)
        readProjectByNameAndClientId: _.readProjectByNameAndClientId()
            output
                setStore1: UIEngine.SetStore(path = `'Page.isOpen'`, value = false) AFTER Steps.readProjectByNameAndClientId.output
                if: System.If(condition = Store.urlDetails.pathParts[1] = undefined) AFTER Steps.readProjectByNameAndClientId.output
                    false
                        if1: System.If(condition = Store.auth.user.id = undefined) AFTER Steps.if.false
                            false
                                getCallDetails: hrms.getCallDetails(projectId = Page.allDetails._id, userId = Store.auth.user.id) AFTER Steps.if1.false
                                    output
                                        setStore: UIEngine.SetStore(path = "Page.table", value = Steps.getCallDetails.output.scheduleCallDetails)
                                            output
                                                setStore5: UIEngine.SetStore(path = `'Page.showPage'`, value = true) AFTER Steps.setStore.output
                setStore6: UIEngine.SetStore(path = "Page.scheduleAcall", value = Page.allDetails.scheduleAcall) AFTER Steps.readProjectByNameAndClientId.output
                    output
                        if2: System.If(condition = Page.scheduleAcall = undefined) AFTER Steps.setStore6.output
                            true
                                setStore7: UIEngine.SetStore(path = "Page.showGrid", value = true) AFTER Steps.if2.true
                            false
                                setStore7_Copy_1: UIEngine.SetStore(path = "Page.showGrid", value = false) AFTER Steps.if2.false