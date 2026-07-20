FUNCTION customFieldAdding
    LOGIC
        ifChecked: System.If(condition = Parent.isSelected)
            true
                insertLast: System.Array.InsertLast(element = Parent, source = Page.addedCustomFields) AFTER Steps.ifChecked.true
                    output
                        addedCustomFields: UIEngine.SetStore(path = "Page.addedCustomFields", value = Steps.insertLast.output.result)
            false
                forEachLoop: System.Loop.ForEachLoop(source = Page.addedCustomFields) AFTER Steps.ifChecked.false
                    iteration
                        ifSameName: System.If(condition = Steps.forEachLoop.iteration.each.name = Parent.name)
                            true
                                delete: System.Array.Delete(source = Page.addedCustomFields, element = Page.addedCustomFields[Steps.forEachLoop.iteration.index]) AFTER Steps.ifSameName.true
                                    output
                                        newData: UIEngine.SetStore(path = "Page.addedCustomFields", value = Steps.delete.output.result)