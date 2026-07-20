FUNCTION submitProject
    LOGIC
        print: System.Print(values = Page.obj)
            output
                create: CoreServices.Storage.Create(storageName = "rim project", dataObject = Page.obj) AFTER Steps.print.output
                    error
                        print2: System.Print(values = Steps.create.error.result)
                    output
                        print1: System.Print(values = Steps.create.output.result)