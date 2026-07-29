FUNCTION ProjectHighlights
    LOGIC
        insertLast: System.Array.InsertLast(source = Page.projectHighlights.highlight, element = `""`)
            output
                setStore: UIEngine.SetStore(path = "Page.projectHighlights.highlight", value = Steps.insertLast.output.result)