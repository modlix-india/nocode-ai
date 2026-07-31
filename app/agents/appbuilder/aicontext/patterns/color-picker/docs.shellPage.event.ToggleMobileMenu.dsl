FUNCTION ToggleMobileMenu
    LOGIC
        setStore: UIEngine.SetStore(path = "Store.pageData._global.showMenu", value = not Store.pageData._global.showMenu)