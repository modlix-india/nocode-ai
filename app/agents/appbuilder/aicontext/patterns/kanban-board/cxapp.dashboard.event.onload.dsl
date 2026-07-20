FUNCTION onload
    LOGIC
        getCurrentUserKYCs: kyc.getCurrentUserKYCs()
            output
                setStore3: UIEngine.SetStore(path = "Page.kycDetails", value = Steps.getCurrentUserKYCs.output.kycDetails)
                    output
                        if1: System.If(condition = Page.kycDetails.length ?? 0 = 0) AFTER Steps.setStore3.output
                            true
                                setStore4: UIEngine.SetStore(path = "Page.showGrid", value = "") AFTER Steps.if1.true
                            false
                                setStore4_Copy_1: UIEngine.SetStore(path = "Page.showGrid", value = "kyc") AFTER Steps.if1.false
        setStore6: UIEngine.SetStore(path = "Page.changeNumberPopup", value = `false`)
            output
                setStore5: UIEngine.SetStore(path = "Page.showSpinner", value = true) AFTER Steps.setStore6.output
                    output
                        readPage: CoreServices.Storage.ReadPage(appCode = "rim", storageName = "Project", size = 200) AFTER Steps.setStore5.output
                            output
                                setStore2: UIEngine.SetStore(path = "Page.projects", value = Steps.readPage.output.result.content)
                                    output
                                        if: System.If(condition = Page.projects = undefined or Page.projects.length = 0) AFTER Steps.setStore2.output
                                            true
                                                setStore: UIEngine.SetStore(path = "Page.display", value = "noProjects") AFTER Steps.if.true
                                            false
                                                setStore_Copy_1: UIEngine.SetStore(path = "Page.display", value = "projects") AFTER Steps.if.false
                                                    output
                                                        wait: System.Wait(millis = 1000) AFTER Steps.setStore_Copy_1.output
                                                            output
                                                                setStore5_Copy_1: UIEngine.SetStore(path = "Page.showSpinner", value = false) AFTER Steps.wait.output