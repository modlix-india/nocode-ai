FUNCTION fetchInvitedUser
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.confirmGrid", value = `"dontShow"`)
        fetchData: UIEngine.FetchData(url = `'api/security/users/inviteDetails/{{Store.urlDetails.pathParts[1]}}'`)
            output
                if: System.If(condition = `Steps.fetchData.output.data = ""`) AFTER Steps.fetchData.output
                    true
                        setStore: UIEngine.SetStore(path = "Page.invitedUserData", value = {}) AFTER Steps.if.true
                    false
                        data: UIEngine.SetStore(path = "Page.invitedUserData", value = Steps.fetchData.output.data) AFTER Steps.if.false
        privacyDetails: _.privacyDetails()