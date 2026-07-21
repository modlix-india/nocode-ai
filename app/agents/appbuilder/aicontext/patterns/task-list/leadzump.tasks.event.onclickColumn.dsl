FUNCTION onclickColumn
    LOGIC
        columns: UIEngine.SetStore(path = "Page.columns", value = not Page.columns)
            output
                filter: UIEngine.SetStore(path = "Page.filter", value = false) AFTER Steps.columns.output