FUNCTION loginFunction
    LOGIC
        login: UIEngine.Login(userName = Page.loginForm.userName, rememberMe = Page.loginForm.rememberMe, identifierType = Page.loginForm.identifierType, userId = Page.loginForm.userId)
            error
                if: System.If(condition = Steps.login.error.status >= 400)
                    true
                        setStore: UIEngine.SetStore(path = "Page.signInError", value = `true`) AFTER Steps.if.true