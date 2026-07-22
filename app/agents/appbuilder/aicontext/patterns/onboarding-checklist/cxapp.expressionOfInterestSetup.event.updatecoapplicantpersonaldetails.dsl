FUNCTION updatecoapplicantpersonaldetails
    LOGIC
        if: System.If(condition = Page.coapplicantalldetails)
            true
                objectKeys1: System.Object.ObjectKeys(source = Page.EOI[0].coApplicantPersonal) AFTER Steps.if.true
                    output
                        forEachLoop: System.Loop.ForEachLoop(source = Steps.objectKeys1.output.value)
                            iteration
                                setStore1: UIEngine.SetStore(path = `'Page.EOI[0].coApplicantPersonal.{{Steps.forEachLoop.iteration.each}}'`, value = true)
                                    output
                                        setStore2: UIEngine.SetStore(path = "Page.coapplicantpersonaltotal", value = 5) AFTER Steps.setStore1.output
            false
                objectkeys2: System.Object.ObjectKeys(source = Page.EOI[0].coApplicantPersonal) AFTER Steps.if.false
                    output
                        forEachLoop1: System.Loop.ForEachLoop(source = Steps.objectkeys2.output.value)
                            iteration
                                setStore3: UIEngine.SetStore(path = `'Page.EOI[0].coApplicantPersonal.{{Steps.forEachLoop1.iteration.each}}'`, value = false)
                                    output
                                        setStore2_Copy_1: UIEngine.SetStore(path = "Page.coapplicantpersonaltotal", value = 0) AFTER Steps.setStore3.output