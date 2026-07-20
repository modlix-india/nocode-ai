FUNCTION onLoad
    LOGIC
        if: System.If(condition = Store.urlDetails.pathParts.length > 2)
            true
                read: CoreServices.Storage.Read(storageName = "blogs", dataObjectId = Store.urlDetails.pathParts[1]) AFTER Steps.if.true
                    output
                        setStore: UIEngine.SetStore(path = "Page.blogData", value = Steps.read.output.result)
            false
                setStore1: UIEngine.SetStore(path = "Page.filterBlog", value = {
    "field": "slug"
}) AFTER Steps.if.false
                    output
                        setStore2: UIEngine.SetStore(path = "Page.filterBlog.value", value = Store.urlDetails.pathParts[1]) AFTER Steps.setStore1.output
                            output
                                readPage: CoreServices.Storage.ReadPage(storageName = "blogs", filter = Page.filterBlog, size = 1, count = `false`) AFTER Steps.setStore2.output
                                    output
                                        setStore_Copy_1: UIEngine.SetStore(path = "Page.blogData", value = Steps.readPage.output.result.content[0])