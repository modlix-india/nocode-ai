FUNCTION signUp
    LOGIC
        navigate: UIEngine.Navigate(linkPath = `'https://{{Page.urlPrefix}}authzump.ai/appSignUp?appCode=leadzump&clientCode=SYSTEM&redirectUrl=https://{{Page.urlPrefix}}leadzump.ai/deals'`, force = true)