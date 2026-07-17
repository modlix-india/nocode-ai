FUNCTION OnfocusPasswordAndBlur
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.showStrengthIndicator", value = `Page.showStrengthIndicator = "strengthIndicator" ? 'nochange' :"strengthIndicator"`)
        if: System.If(condition = Page.lenTrue = 6)
            true
                setStore_Copy_1: UIEngine.SetStore(path = "Page.passwordValidationBox", value = false) AFTER Steps.if.true
            false
                setStore_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.passwordValidationBox", value = true) AFTER Steps.if.false