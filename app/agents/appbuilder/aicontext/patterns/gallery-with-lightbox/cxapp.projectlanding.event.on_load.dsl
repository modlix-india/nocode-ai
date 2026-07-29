FUNCTION on_load
    LOGIC
        setStore3_Copy_1: UIEngine.SetStore(path = `'Page.bookingDetails.projectId'`, value = Store.urlDetails.pathParts[1])
        readProjectByNameAndClientId: _.readProjectByNameAndClientId()
            output
                setStore3: UIEngine.SetStore(path = `'Page.bookingDetails.amountInvested'`, value = Page.project.inventoryManagement.minimumAmount) AFTER Steps.readProjectByNameAndClientId.output
                    output
                        if1: System.If(condition = Page.bookingDetails.amountInvested = null) AFTER Steps.setStore3.output
                            false
                                setStore_Copy_2: UIEngine.SetStore(path = "Page.bookingDetails.sqftAllocated", value = {{Page.bookingDetails.amountInvested ?? 0}} / {{Page.project.inventoryManagement.amountPerSqft ?? 1}}) AFTER Steps.if1.false
                if: System.If(condition = Page.project.gallery.images) AFTER Steps.readProjectByNameAndClientId.output
                    true
                        minimum: System.Math.Minimum(value = 3, value = Page.project.gallery.images.length ?? 0) AFTER Steps.if.true
                            output
                                subArray: System.Array.SubArray(source = Page.project.gallery.images, length = Steps.minimum.output.value)
                                    output
                                        setStore: UIEngine.SetStore(path = "Page.gallery.images", value = Steps.subArray.output.result)
                                            output
                                                insertLast: System.Array.InsertLast(source = Page.project.gallery.images, element = Page.project.projectInfo.projectBackgroundImage) AFTER Steps.setStore.output
                                                    output
                                                        setStore1: UIEngine.SetStore(path = "Page.project.gallery.images", value = Steps.insertLast.output.result)
                if2: System.If(condition = Page.project.specifications) AFTER Steps.readProjectByNameAndClientId.output
                    true
                        ceiling: System.Math.Ceiling(value = Page.project.specifications.length / 2) AFTER Steps.if2.true
                            output
                                subArray1: System.Array.SubArray(source = Page.project.specifications, length = Steps.ceiling.output.value)
                                    output
                                        setStore4: UIEngine.SetStore(path = "Page.specificationFirstHalf", value = Steps.subArray1.output.result)
                                subArray2: System.Array.SubArray(source = Page.project.specifications, findFrom = Steps.ceiling.output.value)
                                    output
                                        setStore5: UIEngine.SetStore(path = "Page.specificationSecondHalf", value = Steps.subArray2.output.result)