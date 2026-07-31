FUNCTION onClickActivityButton
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.activeTabButton", value = "activity")
            output
                if: System.If(condition = Page.activityLogs = undefined) AFTER Steps.setStore.output
                    true
                        gettingActivityLogs: _.gettingActivityLogs() AFTER Steps.if.true
                dataNew: UIEngine.SetStore(value = Page.callLogsData, path = "Page.callLogsDataSubArray") AFTER Steps.setStore.output