FUNCTION New_Function
    LOGIC
        setStore: UIEngine.SetStore(path = "Parent.isVisible", value = not Parent.isVisible)
            output
                if: System.If(condition = Parent.isVisible) AFTER Steps.setStore.output
                    true
                        setStore1: UIEngine.SetStore(path = "Page.openCount", value = (Page.openCount ?? 0) + 1) AFTER Steps.if.true
                    false
                        setStore1_Copy_1: UIEngine.SetStore(path = "Page.openCount", value = (Page.openCount ?? 0) - 1) AFTER Steps.if.false