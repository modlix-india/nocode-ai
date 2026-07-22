FUNCTION onClickEmail
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.isMailSelected", value = not {{Page.isMailSelected ?? false}})
            output
                if: System.If(condition = `Page.showQuestion = 'Question3'`) AFTER Steps.setStore1.output
                    true
                        setStore_Copy_1: UIEngine.SetStore(path = "Page.showQuestion", value = "Question2") AFTER Steps.if.true
                    false
                        setStore: UIEngine.SetStore(path = "Page.showQuestion", value = "Question3") AFTER Steps.if.false