FUNCTION OnLoad
    LOGIC
        if: System.If(condition = Store.currentApp) /* If the current app is set then */
            true
                setStore: UIEngine.SetStore(path = "Page.app", value = Store.appDefs.{{Store.currentApp}}) AFTER Steps.if.true /* Move it to Page storage for easy access */