FUNCTION emptyScreenFunction
    LOGIC
        if: System.If(condition = Page.projectUpdates.content.length = 0)
            true
                setStore2: UIEngine.SetStore(path = "Page.screenVisibility", value = "EMPTYSCREEN") AFTER Steps.if.true
            false
                setStore3: UIEngine.SetStore(path = "Page.screenVisibility", value = "DATASCREEN") AFTER Steps.if.false