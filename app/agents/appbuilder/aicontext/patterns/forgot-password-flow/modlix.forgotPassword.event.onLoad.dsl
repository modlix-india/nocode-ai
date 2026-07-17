FUNCTION onLoad
    LOGIC
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
})
            output
                setStore2: UIEngine.SetStore(path = "Page.policyDetails", value = Steps.fetchData.output.data)
        setStore4: UIEngine.SetStore(path = "Page.showGrid", value = "loader")
            output
                wait: System.Wait(millis = 2000) AFTER Steps.setStore4.output
                    output
                        setStore1: UIEngine.SetStore(path = "Page.showGrid", value = "main") AFTER Steps.wait.output
                            output
                                setStore: UIEngine.SetStore(path = "Page.showQuestion", value = "Question1") AFTER Steps.setStore1.output
                                    output
                                        setStore3: UIEngine.SetStore(path = "Page.colors", value = [{
    "profile": "api/files/static/file/SYSTEM/MarketingAI/SignIn/profile1.svg",
    "border": "#AFE0F133",
    "hover": "#AFE0F1",
    "color": "#AFE0F11A"
}, {
    "profile": "api/files/static/file/SYSTEM/MarketingAI/SignIn/profile2.svg",
    "border": "#F1E0B733",
    "hover": "#F1E0B7",
    "color": "#F1E0B71A"
}, {
    "profile": "api/files/static/file/SYSTEM/MarketingAI/SignIn/profile3.svg",
    "border": "#CAE2CB33",
    "hover": "#CAE2CB",
    "color": "#CAE2CB1A"
}, {
    "profile": "api/files/static/file/SYSTEM/MarketingAI/SignIn/profile4.svg",
    "border": "#F5CDD233",
    "hover": "#F5CDD2",
    "color": "#F5CDD21A"
}]) AFTER Steps.setStore.output