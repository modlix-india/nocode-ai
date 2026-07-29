FUNCTION ProjectHighlights
    LOGIC
        insertLast: System.Array.InsertLast(source = Page.project.projectHighlights.highlights, element = {
    "name": "",
    "description": "",
    "image": ""
})
            output
                setStore: UIEngine.SetStore(path = "Page.project.projectHighlights.highlights", value = Steps.insertLast.output.result)