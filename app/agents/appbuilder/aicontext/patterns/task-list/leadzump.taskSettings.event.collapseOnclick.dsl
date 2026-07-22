FUNCTION collapseOnclick
    LOGIC
        toggle: UIEngine.SetStore(path = "Page.isHelpVisible", value = not Page.isHelpVisible)