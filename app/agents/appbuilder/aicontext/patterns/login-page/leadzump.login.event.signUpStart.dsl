FUNCTION signUpStart
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.currentState", value = 1)
        setStore1: UIEngine.SetStore(path = "Page.activeTab", value = "SignUp")
        cleanUp: _.cleanUp()