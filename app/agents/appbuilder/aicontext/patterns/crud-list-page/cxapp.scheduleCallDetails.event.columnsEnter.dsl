FUNCTION columnsEnter
    LOGIC
        if: System.If(condition = Page.AllScheduleCalls.total  != 0)
            true
                setStore: UIEngine.SetStore(path = "Page.filters", value = false) AFTER Steps.if.true
                    output
                        setStore3: UIEngine.SetStore(path = "Page.Columns", value = true) AFTER Steps.setStore.output
                            output
                                setStore1: UIEngine.SetStore(path = "Page.calenderGrid", value = false) AFTER Steps.setStore3.output