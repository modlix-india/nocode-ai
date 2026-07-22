FUNCTION showFilterPopupDefination
    NAMESPACE UIApp
    EVENTS
        output
    LOGIC
        showFilter: UIEngine.SetStore(path = "Page.showFilterPopup", value = Page.showFilterPopup ? false : true)
            output
                genOutput: System.GenerateEvent() AFTER Steps.showFilter.output