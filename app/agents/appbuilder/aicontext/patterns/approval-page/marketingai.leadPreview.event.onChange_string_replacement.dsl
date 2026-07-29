FUNCTION onChange_string_replacement
    LOGIC
        forEachLoop: System.Loop.ForEachLoop(source = Page.questionsArray)
            iteration
                forEachLoop1: System.Loop.ForEachLoop(source = Steps.forEachLoop.iteration.each.answers)
                    iteration
                        setStore: UIEngine.SetStore(path = "Page.value", value = Steps.forEachLoop1.iteration.each.value)
                            output
                                Checking_option_empty_or_not: System.If(condition = Page.value.length) AFTER Steps.setStore.output
                                    true
                                        trim: System.String.Trim(string = Page.value) AFTER Steps.Checking_option_empty_or_not.true
                                            output
                                                lowerCase: System.String.LowerCase(string = Steps.trim.output.result)
                                                    output
                                                        replace: System.String.Replace(string = Steps.lowerCase.output.result, secondString = " ", thirdString = "_")
                                                            output
                                                                setStore2_Copy_1: UIEngine.SetStore(path = `'Page.questionsArray[{{Steps.forEachLoop.iteration.index}}].answers[{{Steps.forEachLoop1.iteration.index}}].key'`, value = Steps.replace.output.result)