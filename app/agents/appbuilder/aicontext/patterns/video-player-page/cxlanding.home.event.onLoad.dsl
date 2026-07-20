FUNCTION onLoad
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.toggleGrid1", value = false)
            output
                setStore_Copy_1: UIEngine.SetStore(path = "Page.toggleGrid2", value = false) AFTER Steps.setStore.output
                    output
                        setStore_Copy_2: UIEngine.SetStore(path = "Page.toggleGrid3", value = false) AFTER Steps.setStore_Copy_1.output
                            output
                                setStore_Copy_3: UIEngine.SetStore(path = "Page.toggleGrid4", value = false) AFTER Steps.setStore_Copy_2.output
                                    output
                                        setStore_Copy_4: UIEngine.SetStore(path = "Page.toggleGrid5", value = false) AFTER Steps.setStore_Copy_3.output
                                            output
                                                setStore_Copy_5: UIEngine.SetStore(path = "Page.toggleGrid6", value = false) AFTER Steps.setStore_Copy_4.output
                                                    output
                                                        setStore1: UIEngine.SetStore(path = "Page.popup", value = false) AFTER Steps.setStore_Copy_5.output
                                                            output
                                                                setStore_Copy_7: UIEngine.SetStore(path = `'Page.toggleVideo'`, value = false) AFTER Steps.setStore1.output
                                                                    output
                                                                        onload2: _.onload2() AFTER Steps.setStore_Copy_7.output
                                                                            output
                                                                                setStore_Copy_6: UIEngine.SetStore(path = "Page.toggleGrid7", value = false) AFTER Steps.onload2.output