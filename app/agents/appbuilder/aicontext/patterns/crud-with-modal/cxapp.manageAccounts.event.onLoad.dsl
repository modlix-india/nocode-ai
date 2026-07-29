FUNCTION onLoad
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.isViewDetailsGridOpen", value = false)
        setStore2: UIEngine.SetStore(path = "Page.ShowPopup", value = false)
        setStore5: UIEngine.SetStore(path = "Page.function", value = "Starting")
            output
                readProjectByNameAndClientId: _.readProjectByNameAndClientId() AFTER Steps.setStore5.output
                    output
                        getCurrentUserKYCs: kyc.getCurrentUserKYCs() AFTER Steps.readProjectByNameAndClientId.output
                            output
                                setStore: UIEngine.SetStore(path = "Page.kycUsers", value = Steps.getCurrentUserKYCs.output.kycDetails)
                                    output
                                        forEachLoop: System.Loop.ForEachLoop(source = Page.kycUsers) AFTER Steps.setStore.output
                                            iteration
                                                if: System.If(condition = Steps.forEachLoop.iteration.each.joint != undefined)
                                                    true
                                                        objectKeys: System.Object.ObjectKeys(source = Steps.forEachLoop.iteration.each.joint) AFTER Steps.if.true
                                                            output
                                                                setStore3: UIEngine.SetStore(path = "Page.jointArray", value = Steps.objectKeys.output.value)
                                                                    output
                                                                        setStore4: UIEngine.SetStore(path = `'Page.kycUsers[{{Steps.forEachLoop.iteration.index}}].joint.length'`, value = Page.jointArray.length) AFTER Steps.setStore3.output
                                            output
                                                onloadTab: _.onloadTab() AFTER Steps.forEachLoop.output
                                        insertLast: System.Array.InsertLast(source = Page.kycUsers, element = null) AFTER Steps.setStore.output
                                            output
                                                setStore1_Copy_1: UIEngine.SetStore(path = "Page.kycUsers", value = Steps.insertLast.output.result)