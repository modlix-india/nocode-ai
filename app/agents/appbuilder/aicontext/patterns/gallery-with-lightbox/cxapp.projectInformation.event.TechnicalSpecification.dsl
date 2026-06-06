FUNCTION TechnicalSpecification
    LOGIC
        insertLast: System.Array.InsertLast(source = Page.project.specifications, element = {
    "name": "",
    "description": "",
    "image": ""
})
            output
                setStore: UIEngine.SetStore(path = "Page.project.specifications", value = Steps.insertLast.output.result)