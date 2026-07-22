FUNCTION fetchAllNotifications
    LOGIC
        fetchData: UIEngine.FetchData(url = "/api/notification/notifications", queryParams = {
    "page": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Page.allNotifications.number ?? 0"
        }
    },
    "size": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Page.allNotifications.size ?? 20"
        }
    },
    "appCode": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Store.application.appCode"
        }
    },
    "clientCode": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Store.auth.client.code"
        }
    }
})
            output
                setStore: UIEngine.SetStore(path = "Page.allNotifications", value = Steps.fetchData.output.data)
                    output
                        if: System.If(condition = Page.allNotifications.content) AFTER Steps.setStore.output
                            true
                                setStore1: UIEngine.SetStore(path = "Page.show", value = `"Notification"`) AFTER Steps.if.true
                            false
                                setStore1_Copy_1: UIEngine.SetStore(path = "Page.show", value = `"Empty"`) AFTER Steps.if.false