FUNCTION editMail
    LOGIC
        pageName: UIEngine.SetStore(path = "Page.activePage", value = Page.activePage - 1)
        setStore: UIEngine.SetStore(path = "Page.user.otp", deleteKey = true)
        setStore1: UIEngine.SetStore(path = "Page.validOtpLength", value = `false`)
        timeBreak: UIEngine.SetStore(path = "Page.timmer", value = `"Break"`)
        timeVariable: UIEngine.SetStore(path = "Page.time", value = 15)