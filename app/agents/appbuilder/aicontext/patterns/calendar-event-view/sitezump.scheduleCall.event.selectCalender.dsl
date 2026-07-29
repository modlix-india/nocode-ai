FUNCTION selectCalender
    LOGIC
        if: System.If(condition = `Page.dataPageNumber = "calender"`)
            true
                date: UIEngine.SetStore(path = "Page.dataPageNumber", value = `"time"`) AFTER Steps.if.true
            false
                backT: UIEngine.SetStore(path = "Page.dataPageNumber", value = `"calender"`) AFTER Steps.if.false