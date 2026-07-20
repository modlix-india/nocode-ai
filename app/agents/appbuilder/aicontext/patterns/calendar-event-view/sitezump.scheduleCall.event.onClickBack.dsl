FUNCTION onClickBack
    LOGIC
        if: System.If(condition = `Page.show = "calendar"`)
            true
                navigate: UIEngine.Navigate(linkPath = "/") AFTER Steps.if.true
            false
                setStore: UIEngine.SetStore(path = "Page.show", value = "calendar") AFTER Steps.if.false
        clearValidations: _.clearValidations()