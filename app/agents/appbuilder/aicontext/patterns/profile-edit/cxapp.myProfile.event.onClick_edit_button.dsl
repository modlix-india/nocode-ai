FUNCTION onClick_edit_button
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.mainGrid", value = false)
            output
                setStore: UIEngine.SetStore(path = "Page.showEditGrid", value = true) AFTER Steps.setStore1.output
                    output
                        setStore2: UIEngine.SetStore(path = "Page.changePinGrid ", value = false) AFTER Steps.setStore.output