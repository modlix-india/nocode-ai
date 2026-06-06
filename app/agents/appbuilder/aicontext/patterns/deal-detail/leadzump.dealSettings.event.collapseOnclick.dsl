FUNCTION collapseOnclick
    LOGIC
        isVisible: UIEngine.SetStore(path = "Page.isHelpVisible", value = not Page.isHelpVisible)