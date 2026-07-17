FUNCTION fetchInvitedUser
    LOGIC
        fetchData: UIEngine.FetchData(url = `'api/security/users/inviteDetails/{{Store.urlDetails.pathParts[1]}}'`)
            output
                setStore: UIEngine.SetStore(path = "Page.invitedUserData", value = Steps.fetchData.output.data)
        fetchDataPolicy: UIEngine.FetchData(url = "api/security/clientPasswordPolicy/codes/policy", headers = {
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
                setStore2: UIEngine.SetStore(path = "Page.policyDetails", value = Steps.fetchDataPolicy.output.data)