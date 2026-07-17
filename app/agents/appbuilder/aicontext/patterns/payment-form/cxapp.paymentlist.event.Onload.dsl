FUNCTION Onload
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.paid", value = true)
        setStore1: UIEngine.SetStore(path = "Page.pending", value = false)