FUNCTION DeleteCodePart
    LOGIC
        setStore: UIEngine.SetStore(path = `'Page.app.properties.codeParts.{{Parent.__index}}'`, value = `undefined`, deleteKey = true)