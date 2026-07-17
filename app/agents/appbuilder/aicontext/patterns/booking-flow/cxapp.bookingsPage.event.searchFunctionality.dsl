FUNCTION searchFunctionality
    LOGIC
        if1: System.If(condition = Page.searchString.length != 0 and Page.searchString != undefined)
            true
                setStore: UIEngine.SetStore(path = "Page.temp", value = {}) AFTER Steps.if1.true
                    output
                        objectEntries: System.Object.ObjectEntries(source = Page.columns.data) AFTER Steps.setStore.output
                            output
                                forEachLoop: System.Loop.ForEachLoop(source = Steps.objectEntries.output.value) AFTER Steps.objectEntries.output
                                    iteration
                                        lowerCase: System.String.LowerCase(string = Page.searchString) AFTER Steps.forEachLoop.iteration.each
                                        lowerCase1: System.String.LowerCase(string = Steps.forEachLoop.iteration.each[1].name)
                                            output
                                                contains: System.String.Contains(string = Steps.lowerCase1.output.result, searchString = Steps.lowerCase.output.result)
                                                    output
                                                        if: System.If(condition = Steps.contains.output.result)
                                                            true
                                                                setStore1: UIEngine.SetStore(path = `'Page.temp.{{Steps.forEachLoop.iteration.each[0]}}'`, value = Steps.forEachLoop.iteration.each[1]) AFTER Steps.if.true
                                    output
                                        setStore2: UIEngine.SetStore(path = "Page.filterColumns.data", value = Page.temp) AFTER Steps.forEachLoop.output
            false
                setStore3: UIEngine.SetStore(path = "Page.filterColumns", value = Page.columns) AFTER Steps.if1.false