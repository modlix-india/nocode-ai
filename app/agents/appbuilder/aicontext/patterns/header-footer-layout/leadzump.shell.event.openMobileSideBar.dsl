FUNCTION openMobileSideBar
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.openSidebar", value = not Page.openSidebar)
        getAppLogo: _.getAppLogo()