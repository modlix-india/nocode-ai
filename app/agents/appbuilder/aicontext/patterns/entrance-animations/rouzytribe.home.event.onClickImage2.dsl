FUNCTION onClickImage2
    LOGIC
        setStore2: UIEngine.SetStore(path = "Page.playVideo", value = Page.videos[0])
            output
                setStore1: UIEngine.SetStore(path = "Page.showPopup", value = true) AFTER Steps.setStore2.output