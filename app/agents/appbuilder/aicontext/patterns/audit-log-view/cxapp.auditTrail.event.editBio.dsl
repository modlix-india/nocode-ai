FUNCTION editBio
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.editBio", value = `true`)
            output
                onClickEditProfile: _.onClickEditProfile() AFTER Steps.setStore.output