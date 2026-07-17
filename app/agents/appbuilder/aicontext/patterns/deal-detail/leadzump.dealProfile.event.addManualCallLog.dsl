FUNCTION addManualCallLog
    LOGIC
        manualCallLog: UIEngine.SetStore(path = "Page.manualCallLog", value = not Page.manualCallLog)
        getCurrentTimestamp: System.Date.GetCurrentTimestamp()
            output
                timestampToEpochSeconds: System.Date.TimestampToEpochSeconds(isoTimeStamp = Steps.getCurrentTimestamp.output.isoTimeStamp)
                    output
                        setStore: UIEngine.SetStore(path = "Page.callPayload.callDate", value = Steps.timestampToEpochSeconds.output.result)