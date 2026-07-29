FUNCTION onClickInventoryDetails
    LOGIC
        setStore9: UIEngine.SetStore(path = "Page.loader", value = `false`)
            output
                setStore4: UIEngine.SetStore(path = "Page.emptyTowers", value = `false`) AFTER Steps.setStore9.output
                    output
                        setStore3: UIEngine.SetStore(path = "Page.emptyUnits", value = `false`) AFTER Steps.setStore4.output
                            output
                                setStore: UIEngine.SetStore(path = "Page.selected", value = "inventory") AFTER Steps.setStore3.output
                                    output
                                        setStore1_Copy_1: UIEngine.SetStore(path = "Page.addBlock", value = Page.addBlock ? false : Page.addBlock) AFTER Steps.setStore.output
                                            output
                                                setStore1: UIEngine.SetStore(path = "Page.addTower", value = Page.addTower ? false : Page.addTower) AFTER Steps.setStore1_Copy_1.output
                                                    output
                                                        setStore2: UIEngine.SetStore(path = "Page.addUnit", value = Page.addUnit ? false : Page.addUnit) AFTER Steps.setStore1.output
                                                            output
                                                                if: System.If(condition = Page.project.residential.inventoryManagement.phase) AFTER Steps.setStore2.output
                                                                    true
                                                                        setStore5: UIEngine.SetStore(path = `'Page.phases'`, value = Page.project.residential.inventoryManagement.phase) AFTER Steps.if.true
                                                                            output
                                                                                setStore7: UIEngine.SetStore(path = "Page.phaseLength", value = Page.project.residential.inventoryManagement.phase.length) AFTER Steps.setStore5.output
                                                                                    output
                                                                                        setStore8: UIEngine.SetStore(path = "Page.phaseOriginal", value = Page.phases) AFTER Steps.setStore7.output
                                                                                            output
                                                                                                onClickPhasePagination: _.onClickPhasePagination() AFTER Steps.setStore8.output
                                                                    false
                                                                        setStore6: UIEngine.SetStore(path = "Page.phases", value = []) AFTER Steps.if.false
                                                                    output
                                                                        setStore10: UIEngine.SetStore(path = "Page.loader", value = `true`) AFTER Steps.if.output