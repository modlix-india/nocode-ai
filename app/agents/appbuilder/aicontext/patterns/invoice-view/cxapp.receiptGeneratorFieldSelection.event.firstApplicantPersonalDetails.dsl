FUNCTION firstApplicantPersonalDetails
    LOGIC
        if: System.If(condition = Page.firstApplicantPersonalAllDetails)
            true
                objectKeys1: System.Object.ObjectKeys(source = Page.bookingFormSetup.personalDetails.firstApplicantPersonalDetails) AFTER Steps.if.true
                    output
                        forEachLoop: System.Loop.ForEachLoop(source = Steps.objectKeys1.output.value)
                            iteration
                                setStore1: UIEngine.SetStore(path = `'Page.bookingFormSetup.personalDetails.firstApplicantPersonalDetails.{{Steps.forEachLoop.iteration.each}}'`, value = true)
                                    output
                                        setStore2: UIEngine.SetStore(path = "Page.firstApplicantPersonalTotal", value = 8) AFTER Steps.setStore1.output
            false
                objectkeys2: System.Object.ObjectKeys(source = Page.bookingFormSetup.personalDetails.firstApplicantPersonalDetails) AFTER Steps.if.false
                    output
                        forEachLoop1: System.Loop.ForEachLoop(source = Steps.objectkeys2.output.value)
                            iteration
                                setStore3: UIEngine.SetStore(path = `'Page.bookingFormSetup.personalDetails.firstApplicantPersonalDetails.{{Steps.forEachLoop1.iteration.each}}'`, value = false)
                                    output
                                        setStore2_Copy_1: UIEngine.SetStore(path = "Page.firstApplicantPersonalTotal", value = 0) AFTER Steps.setStore3.output