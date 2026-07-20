FUNCTION onLoad
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.notificationsTab", value = "All notifications")
        fetchAllNotifications: _.fetchAllNotifications()