FUNCTION signInStart
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.currentState", value = 4)
        setStore1: UIEngine.SetStore(path = "Page.activeTab", value = "SignIn")
        cleanUp: _.cleanUp()