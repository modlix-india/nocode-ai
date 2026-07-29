FUNCTION onOpenPhase
    LOGIC
        setStore: UIEngine.SetStore(path = `'Parent.open'`, value = not Parent.open)