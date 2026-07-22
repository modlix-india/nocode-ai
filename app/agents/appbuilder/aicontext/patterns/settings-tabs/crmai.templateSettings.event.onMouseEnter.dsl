FUNCTION onMouseEnter
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.activeTemplateId", value = Parent.id)