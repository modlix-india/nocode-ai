FUNCTION Onload
    LOGIC
        settingIndividualdefaultvalue: UIEngine.SetStore(path = "Page.user.clientType", value = `"Individual"`)
            output
                fetchData: UIEngine.FetchData(url = "api/security/clientPasswordPolicy/codes/policy", headers = {
    "Authorization": {
        "location": {
            "expression": "LocalStore.AuthToken",
            "type": "EXPRESSION"
        }
    },
    "clientCode": {
        "location": {
            "expression": "Store.auth.loggedInClientCode",
            "type": "EXPRESSION"
        }
    },
    "appCode": {
        "location": {
            "expression": "Store.urlDetails.queryParameters.appCode",
            "type": "EXPRESSION"
        }
    }
}) AFTER Steps.settingIndividualdefaultvalue.output
                    output
                        setStore4: UIEngine.SetStore(path = "Page.policyDetails", value = Steps.fetchData.output.data)
                            output
                                if2: System.If(condition = Store.urlDetails.queryParameters.emailId) AFTER Steps.setStore4.output
                                    true
                                        setStore2: UIEngine.SetStore(path = "Page.user.name", value = `Store.urlDetails.queryParameters.clientName ?? ""`) AFTER Steps.if2.true
                                            output
                                                signUpOnLoad: Authzump.sso.SignUpOnLoad(phoneNumber = "", email = Store.urlDetails.queryParameters.emailId) AFTER Steps.setStore2.output
                                                    error
                                                        if1: System.If(condition = Steps.signUpOnLoad.error.status = 409)
                                                            true
                                                                setStore1_Copy_2: UIEngine.SetStore(path = "Page.showGrid", value = "main") AFTER Steps.if1.true
                                                                    output
                                                                        setStore_Copy_1: UIEngine.SetStore(path = "Page.showQuestion", value = "Question2") AFTER Steps.setStore1_Copy_2.output
                                                                            output
                                                                                setStore3: UIEngine.SetStore(path = "Page.userExistsError", value = "User Already Exists") AFTER Steps.setStore_Copy_1.output
                                                    output
                                                        if: System.If(condition = Steps.signUpOnLoad.output.data = false)
                                                            true
                                                                socialRegister: Authzump.sso.SocialRegister(socialRegisterState = Store.urlDetails.queryParameters.sessionId, firstName = Store.urlDetails.queryParameters.firstName, lastName = `Store.urlDetails.queryParameters.lastName ?? ""`, clientName = Page.user.name, clientCode = "SYSTEM", emailId = Store.urlDetails.queryParameters.emailId, appCode = "marketingai") AFTER Steps.if.true
                                                                    output
                                                                        oneTimeLogin: Authzump.sso.OneTimeLogin(userName = Store.urlDetails.queryParameters.emailId) AFTER Steps.socialRegister.output
                                    false
                                        setStore1: UIEngine.SetStore(path = "Page.showGrid", value = "main") AFTER Steps.if2.false
                                            output
                                                setStore: UIEngine.SetStore(path = "Page.showQuestion", value = "Question1") AFTER Steps.setStore1.output