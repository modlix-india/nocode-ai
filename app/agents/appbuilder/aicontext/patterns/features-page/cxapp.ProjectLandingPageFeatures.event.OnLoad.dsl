FUNCTION OnLoad
    LOGIC
        setStore8: UIEngine.SetStore(path = "Page.projectHighlights", value = {
    "highlight": [
        "",
        ""
    ]
})
        setStore8_Copy_1: UIEngine.SetStore(path = "Page.specifications", value = [{
    "specification": "",
    "description": ""
}])
        setStore: UIEngine.SetStore(path = "Page.amenities", value = [{
    "amenitysvg": "",
    "amenityDescription": ""
}])