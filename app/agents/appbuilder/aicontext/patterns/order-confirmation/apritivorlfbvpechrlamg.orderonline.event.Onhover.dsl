FUNCTION Onhover
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.onhover", value = Parent.__index, deleteKey = true)