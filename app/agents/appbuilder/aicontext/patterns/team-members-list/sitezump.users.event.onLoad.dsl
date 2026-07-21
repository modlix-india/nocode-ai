FUNCTION onLoad
    LOGIC
        onClickUsersToggle: _.onClickUsersToggle()
        fetchInvitedUsers: _.fetchInvitedUsers()
        fetchUsers: _.fetchUsers()
        if: System.If(condition = `Store.urlDetails.queryParameters.userRequest = "true"`)
            true
                setStore: UIEngine.SetStore(path = "Page.userRequestPopup", value = `true`) AFTER Steps.if.true
                loadProfiles: _.loadProfiles() AFTER Steps.if.true
                fetchData: UIEngine.FetchData(url = `'/api/security/users/requestUser/{{Store.urlDetails.pathParts[1]}}'`) AFTER Steps.if.true
                    output
                        setStore1: UIEngine.SetStore(path = "Page.requestUserData", value = Steps.fetchData.output.data)