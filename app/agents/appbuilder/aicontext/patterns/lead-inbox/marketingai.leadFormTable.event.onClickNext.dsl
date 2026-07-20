FUNCTION onClickNext
    LOGIC
        if: System.If(condition = Page.Data.length = Page.index)
            true
                insertLast: System.Array.InsertLast(source = Page.Data, element = {}) AFTER Steps.if.true
                    output
                        setStore2: UIEngine.SetStore(path = "Page.Data", value = Steps.insertLast.output.result)
                            output
                                setStore3: UIEngine.SetStore(path = "Page.index", value = {{Page.index ?? 0 }} + 1) AFTER Steps.setStore2.output
            false
                setStore: UIEngine.SetStore(path = `'Page.index'`, value = Page.index + 1) AFTER Steps.if.false