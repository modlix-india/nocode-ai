FUNCTION onCancelCall
    LOGIC
        forEachLoop: System.Loop.ForEachLoop(source = Page.table.content)
            iteration
                if: System.If(condition = Page.table.content[Steps.forEachLoop.iteration.index]._id = Parent._id)
                    true
                        delete: System.Array.Delete(source = Page.table.content, element = Page.table.content[Steps.forEachLoop.iteration.index]) AFTER Steps.if.true
                            output
                                setStore: UIEngine.SetStore(path = `'Page.table.content'`, value = Steps.delete.output.result)