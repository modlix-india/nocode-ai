FUNCTION save_function
    LOGIC
        setStore5: UIEngine.SetStore(path = "Page.cp", value = {})
            output
                if: System.If(condition = Page.arrayData.length > 0) AFTER Steps.setStore5.output
                    true
                        forEachLoop: System.Loop.ForEachLoop(source = Page.arrayData) AFTER Steps.if.true
                            iteration
                                setStore3: UIEngine.SetStore(path = `'Page.arrayData[{{Steps.forEachLoop.iteration.index}}].uuid'`, value = Page.tempID)
                                if2: System.If(condition = `Steps.forEachLoop.iteration.each.part != undefined and Steps.forEachLoop.iteration.each.part != '' `)
                                    true
                                        if1: System.If(condition = `Steps.forEachLoop.iteration.each.uuid != undefined and Steps.forEachLoop.iteration.each.uuid != '' `) AFTER Steps.if2.true
                                            true
                                                setStore: UIEngine.SetStore(path = "Page.tempID", value = Steps.forEachLoop.iteration.each.uuid) AFTER Steps.if1.true
                                                    output
                                                        setStore2: UIEngine.SetStore(path = `'Page.cp.{{Page.tempID}}.part'`, value = Steps.forEachLoop.iteration.each.part) AFTER Steps.setStore.output
                                                        setStore2_Copy_1: UIEngine.SetStore(path = `'Page.cp.{{Page.tempID}}.order'`, value = Steps.forEachLoop.iteration.index) AFTER Steps.setStore.output
                                                        setStore2_Copy_1_Copy_1: UIEngine.SetStore(path = `'Page.cp.{{Page.tempID}}.place'`, value = `Steps.forEachLoop.iteration.each.place ?? 'BEFORE_HEAD'`) AFTER Steps.setStore.output
                                            false
                                                shortUniqueId: UIEngine.ShortUniqueId() AFTER Steps.if1.false
                                                    output
                                                        setStore1: UIEngine.SetStore(path = "Page.tempID", value = Steps.shortUniqueId.output.id)
                                                            output
                                                                setStore2_Copy_1_Copy_2: UIEngine.SetStore(path = `'Page.cp.{{Page.tempID}}.order'`, value = Steps.forEachLoop.iteration.index) AFTER Steps.setStore1.output
                                                                setStore2_Copy_1_Copy_1_Copy_1: UIEngine.SetStore(path = `'Page.cp.{{Page.tempID}}.place'`, value = `Steps.forEachLoop.iteration.each.place ?? 'BEFORE_HEAD'`) AFTER Steps.setStore1.output
                                                                setStore2_Copy_2: UIEngine.SetStore(path = `'Page.cp.{{Page.tempID}}.part'`, value = Steps.forEachLoop.iteration.each.part) AFTER Steps.setStore1.output
                            output
                                setStore4: UIEngine.SetStore(path = "Page.applicationDefinition.properties.codeParts", value = Page.cp) AFTER Steps.forEachLoop.output
                                    output
                                        sendData: UIEngine.SendData(url = `"api/ui/applications/{{Page.applicationDefinition.id}}"`, method = "PUT", payload = Page.applicationDefinition) AFTER Steps.setStore4.output
                    false
                        setStore4_Copy_1: UIEngine.SetStore(path = "Page.applicationDefinition.properties.codeParts", value = Page.cp) AFTER Steps.if.false
                            output
                                sendData_Copy_1: UIEngine.SendData(url = `"api/ui/applications/{{Page.applicationDefinition.id}}"`, method = "PUT", payload = Page.applicationDefinition) AFTER Steps.setStore4_Copy_1.output