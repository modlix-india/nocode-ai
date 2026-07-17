FUNCTION SaveAndContinue
    LOGIC
        if: System.If(condition = Page.count = 3)
            true
                setStore1: UIEngine.SetStore(path = "Page.buttonText", value = "Finish") AFTER Steps.if.true
            false
                setStore: UIEngine.SetStore(path = "Page.count", value = Page.count +1) AFTER Steps.if.false
                setStore1_Copy_1: UIEngine.SetStore(path = "Page.buttonText", value = "Save & Continue") AFTER Steps.if.false