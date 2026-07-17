FUNCTION loginFunction
    LOGIC
        login: UIEngine.Login(userName = Page.loginForm.userName, rememberMe = Page.loginForm.rememberMe, identifierType = "EMAIL_ID")
            error
                message: UIEngine.Message(msg = Steps.login.error.data)
            output
                if: System.If(condition = Steps.login.output.data!=null)
                    true
                        if1: System.If(condition = Store.auth.client.id = Store.auth.loggedInClientId) AFTER Steps.if.true
                            true
                                navigate: UIEngine.Navigate(linkPath = "/resetNumberAndPin") AFTER Steps.if1.true
                            false
                                navigate_Copy_1: UIEngine.Navigate(linkPath = `'/customerDashboard'`) AFTER Steps.if1.false