FUNCTION Edit
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.individualPayment.paymentPurpose", value = Parent.paymentPurpose)
        setStore1: UIEngine.SetStore(path = "Page.individualPayment.amountPaid", value = Parent.amountPaid)
        setStore2: UIEngine.SetStore(path = "Page.individualPayment.paidOn", value = Parent.paidOn)
        setStore3: UIEngine.SetStore(path = "Page.individualPayment.transactionId", value = Parent.transactionId)
        setStore_Copy_1: UIEngine.SetStore(path = "Page.individualPayment.status", value = Parent.status)
        setStore4: UIEngine.SetStore(path = "Page.individualPayment.paymentMode", value = Parent.paymentMode)
        setStore5: UIEngine.SetStore(path = "Page.edit", value = true)
        setStore6: UIEngine.SetStore(value = Parent.__index, path = "Page.index")