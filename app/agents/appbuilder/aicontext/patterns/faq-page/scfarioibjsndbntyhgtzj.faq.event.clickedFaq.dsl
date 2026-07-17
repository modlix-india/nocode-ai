FUNCTION clickedFaq
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.clickedFaq", value = `Page.clickedFaq = Parent.id ? 'null' : Parent.id`)