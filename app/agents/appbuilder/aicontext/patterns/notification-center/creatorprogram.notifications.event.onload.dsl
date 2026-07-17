FUNCTION onload
    LOGIC
        fetchingLogs: creatorprogram.fetchingLogs()
            output
                setStore3: UIEngine.SetStore(path = "Page.logs", value = Steps.fetchingLogs.output.result)