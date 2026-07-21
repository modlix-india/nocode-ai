FUNCTION on_click_visbile_grid
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.index", value = Parent.__index)
            output
                forEachLoop: System.Loop.ForEachLoop(source = Page.FilledMultipleQuestions) AFTER Steps.setStore.output
                    iteration
                        if: System.If(condition = Steps.forEachLoop.iteration.index = Page.index)
                            true
                                delete: System.Array.Delete(source = Page.FilledMultipleQuestions, element = Page.index) AFTER Steps.if.true
                            false
                                insertLast: System.Array.InsertLast(source = Page.FilledMultipleQuestions, element = Page.index) AFTER Steps.if.false