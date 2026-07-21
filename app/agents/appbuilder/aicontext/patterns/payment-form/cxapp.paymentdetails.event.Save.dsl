FUNCTION Save
    LOGIC
        if: System.If(condition = Page.edit)
            true
                setStore2: UIEngine.SetStore(path = `'Page.paymentDetails[{{Page.index}}]'`, value = Page.individualPayment) AFTER Steps.if.true
                    output
                        setStore3: UIEngine.SetStore(path = "Page.individualPayment", value = {}) AFTER Steps.setStore2.output
                            output
                                setStore4: UIEngine.SetStore(path = "Page.edit", value = false) AFTER Steps.setStore3.output
            false
                insertLast: System.Array.InsertLast(source = Page.paymentDetails, element = Page.individualPayment) AFTER Steps.if.false
                    output
                        setStore: UIEngine.SetStore(path = "Page.paymentDetails", value = Steps.insertLast.output.result)
                            output
                                setStore1: UIEngine.SetStore(path = "Page.individualPayment", value = {}) AFTER Steps.setStore.output