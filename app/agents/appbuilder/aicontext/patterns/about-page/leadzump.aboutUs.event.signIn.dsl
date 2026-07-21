FUNCTION signIn
    LOGIC
        navigate: UIEngine.Navigate(linkPath = `'https://{{Page.urlPrefix}}authzump.ai/appLogin?appCode=leadzump&clientCode=SYSTEM&redirectUrl=https://{{Page.urlPrefix}}leadzump.ai/deals'`, force = true)