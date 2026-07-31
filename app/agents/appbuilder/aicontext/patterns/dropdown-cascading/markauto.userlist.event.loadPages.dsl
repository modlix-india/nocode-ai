FUNCTION loadPages
    LOGIC
        fetchApps: UIEngine.FetchData(url = "api/core/data/UserStorage", queryParams = {
    "page": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Page.pages.number ?? 0"
        }
    },
    "size": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Page.pages.size ?? 10"
        }
    }
})
            error
                messageStep: UIEngine.Message(msg = Steps.fetchApps.error.data)
            output
                storeApps: UIEngine.SetStore(path = "Page.pages", value = Steps.fetchApps.output.data)
                    output
                        genOutput: System.GenerateEvent() AFTER Steps.storeApps.output