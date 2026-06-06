FUNCTION TechnicalSpecification
    LOGIC
        insertLast: System.Array.InsertLast(source = Page.specifications, element = {
    "specification": "",
    "description": ""
})
            output
                setStore: UIEngine.SetStore(path = "Page.specifications", value = Steps.insertLast.output.result)
                    output
                        print: System.Print(values = Page.specifications) AFTER Steps.setStore.output