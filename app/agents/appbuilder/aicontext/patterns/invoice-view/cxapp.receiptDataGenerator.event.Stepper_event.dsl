FUNCTION Stepper_event
    LOGIC
        if: System.If(condition = Page.project.specifications = undefined and Page.count = 1)
            true
                setStore8_c: UIEngine.SetStore(path = "Page.project.specifications", value = [{
    "name": "",
    "description": "",
    "image": ""
}]) AFTER Steps.if.true
        if1: System.If(condition = Page.project.amenities = undefined and Page.count =1)
            true
                setStore: UIEngine.SetStore(path = "Page.project.amenities", value = [{
    "name": "",
    "description": "",
    "image": ""
}]) AFTER Steps.if1.true
        if2: System.If(condition = Page.project.projectHighlights.highlights = undefined and Page.count = 1)
            true
                setStore1: UIEngine.SetStore(path = "Page.project.projectHighlights.highlights", value = [{
    "name": "",
    "description": "",
    "image": ""
}]) AFTER Steps.if2.true
        if3: System.If(condition = Page.project.gallery.images = undefined and Page.count =2)
            true
                setStore2: UIEngine.SetStore(path = "Page.project.gallery.images", value = []) AFTER Steps.if3.true
        if4: System.If(condition = Page.project.location.nearByLocations = undefined and Page.count = 4 )
            true
                setStore3: UIEngine.SetStore(path = "Page.project.location.nearByLocations", value = [{
    "name": "",
    "description": "",
    "image": ""
}]) AFTER Steps.if4.true