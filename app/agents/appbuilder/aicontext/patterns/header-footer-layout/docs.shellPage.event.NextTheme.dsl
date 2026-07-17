FUNCTION NextTheme
    LOGIC
        setStore: UIEngine.SetStore(path = "Store.pageData._global.currentTheme", value = (Store.pageData._global.currentTheme + 1) % Store.pageData._global.details.theme.length)
            output
                setStore1: UIEngine.SetStore(path = "LocalStore.currentTheme", value = Store.pageData._global.currentTheme) AFTER Steps.setStore.output