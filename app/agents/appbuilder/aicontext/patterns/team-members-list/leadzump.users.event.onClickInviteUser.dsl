FUNCTION onClickInviteUser
    LOGIC
        setStore4: UIEngine.SetStore(path = "Page.addorEdit", value = "add")
            output
                setStore1: UIEngine.SetStore(path = "Page.profileData", value = null, deleteKey = true) AFTER Steps.setStore4.output
                    output
                        setStore: UIEngine.SetStore(path = "Page.inviteUserData", value = null, deleteKey = true) AFTER Steps.setStore1.output
                            output
                                clearValidations: _.clearValidations() AFTER Steps.setStore.output
                                    output
                                        inviteUser: UIEngine.SetStore(path = "Page.inviteUser", value = not Page.inviteUser) AFTER Steps.clearValidations.output
        if1: System.If(condition = Page.profiles)
            false
                loadProfiles: _.loadProfiles() AFTER Steps.if1.false
        if: System.If(condition = Page.dropDownUsers)
            false
                fetchUsersForDropDown: _.fetchUsersForDropDown() AFTER Steps.if.false
        if2: System.If(condition = Page.designations)
            false
                fetchingDesignation: _.fetchingDesignation() AFTER Steps.if2.false