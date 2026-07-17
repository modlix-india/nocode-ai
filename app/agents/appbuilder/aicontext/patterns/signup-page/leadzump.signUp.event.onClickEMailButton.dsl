FUNCTION onClickEMailButton
    LOGIC
        showEmail: UIEngine.SetStore(path = "Page.showEmail", value = `"show"`)
        name: UIEngine.SetStore(path = "Page.activePage", value = `1`)