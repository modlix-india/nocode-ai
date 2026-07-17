FUNCTION FirstApplicantUpdate_count
    LOGIC
        objectValues: System.Object.ObjectValues(source = Page.EOI[0].firstApplicantDetails)
            output
                forEachLoop: System.Loop.ForEachLoop(source = Steps.objectValues.output.value)
                    iteration
                        if: System.If(condition = Steps.forEachLoop.iteration.each = true)
                            true
                                setStore: UIEngine.SetStore(path = "Page.tempCount", value = {{Page.tempCount??0}} + 1) AFTER Steps.if.true
                    output
                        setStore1: UIEngine.SetStore(path = "Page.firstApplicantDetailsTotal", value = Page.tempCount) AFTER Steps.forEachLoop.output
                            output
                                setStore1_Copy_1: UIEngine.SetStore(path = "Page.tempCount", value = 0) AFTER Steps.setStore1.output
                                if1: System.If(condition = Page.firstApplicantDetailsTotal >0) AFTER Steps.setStore1.output
                                    true
                                        setStore2_Copy_2: UIEngine.SetStore(path = "Page.firstApplicantAllDetails", value = true) AFTER Steps.if1.true
                                    false
                                        setStore2_Copy_1: UIEngine.SetStore(path = "Page.firstApplicantAllDetails", value = false) AFTER Steps.if1.false