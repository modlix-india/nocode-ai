FUNCTION columnsEnter
    LOGIC
        if: System.If(condition = Page.isCommercial = true ? Page.zeroFilterBookings = false and Page.zeroBookings = true : Page.zeroFilterBookings = false and Page.zeroBookingsResidential = true )
            true
                if3: System.If(condition = Page.Columns = true) AFTER Steps.if.true
                    true
                        oncolumncancel: _.oncolumncancel() AFTER Steps.if3.true
                    false
                        setStore3: UIEngine.SetStore(path = "Page.Columns", value = true) AFTER Steps.if3.false
                            output
                                if1: System.If(condition = Page.filters = true) AFTER Steps.setStore3.output
                                    true
                                        filtersCancelButton: _.FiltersCancelButton() AFTER Steps.if1.true
                                    output
                                        setStore: UIEngine.SetStore(path = "Page.filters", value = false) AFTER Steps.if1.output
                                            output
                                                if2: System.If(condition = Page.isCommercial = true) AFTER Steps.setStore.output
                                                    true
                                                        setStore1_Copy_1: UIEngine.SetStore(path = "Page.previouscolumns", value = Page.columnsCommercial) AFTER Steps.if2.true
                                                            output
                                                                setStore2_Copy_1: UIEngine.SetStore(path = "Page.previousfilterColumns", value = Page.columnsCommercial) AFTER Steps.setStore1_Copy_1.output
                                                                    output
                                                                        setStore3_Copy_2: UIEngine.SetStore(path = "Page.previousisAllcolumnTrue", value = Page.isAllcolumnTrueCommercial) AFTER Steps.setStore2_Copy_1.output
                                                    false
                                                        setStore1_Copy_2: UIEngine.SetStore(path = "Page.previouscolumns", value = Page.columnsResidentail) AFTER Steps.if2.false
                                                            output
                                                                setStore2_Copy_2: UIEngine.SetStore(path = "Page.previousfilterColumns", value = Page.columnsResidentail) AFTER Steps.setStore1_Copy_2.output
                                                                    output
                                                                        setStore3_Copy_3: UIEngine.SetStore(path = "Page.previousisAllcolumnTrue", value = Page.isAllcolumnTrueResidential) AFTER Steps.setStore2_Copy_2.output