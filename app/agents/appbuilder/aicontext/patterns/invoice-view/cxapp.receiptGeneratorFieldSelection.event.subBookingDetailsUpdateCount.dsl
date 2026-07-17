FUNCTION subBookingDetailsUpdateCount
    LOGIC
        objectValues: System.Object.ObjectValues(source = Page.bookingFormSetup.bookingDetails.subBookingDetails)
            output
                forEachLoop: System.Loop.ForEachLoop(source = Steps.objectValues.output.value)
                    iteration
                        if: System.If(condition = Steps.forEachLoop.iteration.each = true)
                            true
                                setStore: UIEngine.SetStore(path = "Page.tempCount", value = {{Page.tempCount??0}} + 1) AFTER Steps.if.true
                    output
                        setStore1: UIEngine.SetStore(path = "Page.BookingDetailsTotal", value = Page.tempCount) AFTER Steps.forEachLoop.output
                            output
                                setStore1_Copy_1: UIEngine.SetStore(path = "Page.tempCount", value = 0) AFTER Steps.setStore1.output
                                if1: System.If(condition = Page.BookingDetailsTotal >0) AFTER Steps.setStore1.output
                                    true
                                        setStore2_Copy_2: UIEngine.SetStore(path = "Page.bookingAllDetails", value = true) AFTER Steps.if1.true
                                    false
                                        setStore2_Copy_1: UIEngine.SetStore(path = "Page.bookingAllDetails", value = false) AFTER Steps.if1.false