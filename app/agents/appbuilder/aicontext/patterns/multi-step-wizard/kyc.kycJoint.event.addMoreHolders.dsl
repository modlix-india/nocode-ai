FUNCTION addMoreHolders
    LOGIC
        insertLast: System.Array.InsertLast(source = Page.joint, element = {})
            output
                setStore: UIEngine.SetStore(path = "Page.joint", value = Steps.insertLast.output.result)