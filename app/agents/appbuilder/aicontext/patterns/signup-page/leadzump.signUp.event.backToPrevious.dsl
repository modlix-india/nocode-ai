FUNCTION backToPrevious
    LOGIC
        countCkeck: System.If(condition = Page.activePage < 0)
            false
                if1: System.If(condition = Page.user.clientType = undefined) AFTER Steps.countCkeck.false
                    true
                        count: UIEngine.SetStore(path = "Page.activePage", value = Page.activePage - 1 ) AFTER Steps.if1.true
                    false
                        if: System.If(condition = `Page.user.clientType = "Individual"`) AFTER Steps.if1.false
                            true
                                count_Copy_2: UIEngine.SetStore(path = "Page.activePage", value = Page.activePage - 2) AFTER Steps.if.true
                            false
                                count_Copy_1: UIEngine.SetStore(path = "Page.activePage", value = Page.activePage - 1 ) AFTER Steps.if.false
        clearValidations: _.clearValidations()
        timeBreak: UIEngine.SetStore(path = "Page.timmer", value = `"Break"`)