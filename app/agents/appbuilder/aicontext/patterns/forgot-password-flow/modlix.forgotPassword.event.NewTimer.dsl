FUNCTION NewTimer
    LOGIC
        if: System.If(condition = Page.time = 0)
            true
                setStore1: UIEngine.SetStore(path = "Page.runtimer", value = false) AFTER Steps.if.true
                    output
                        setStore2: UIEngine.SetStore(path = "Page.showResend", value = true) AFTER Steps.setStore1.output
            false
                setStore: UIEngine.SetStore(path = "Page.time", value = {{Page.time ?? 0}} - 1) AFTER Steps.if.false