FUNCTION getAllTemplatesnew
    LOGIC
        showPreview: UIEngine.SetStore(path = "Page.showPreview", value = false)
        readPage: CoreServices.Storage.ReadPage(storageName = "ClientWhatsappDetails")
            output
                phoneId: UIEngine.SetStore(path = "Page.phoneNumberId", value = Steps.readPage.output.result.content[0].whatsappBusinessId)
                    output
                        size: UIEngine.SetStore(path = "Page.size", value = 10) AFTER Steps.phoneId.output
                            output
                                getAllTemplates: crmai.getTemplates(phoneNumberId = Page.phoneNumberId, navigate = "", size = 10, navigateIndicator  = "after", navigateIndicator = "after") AFTER Steps.size.output
                                    output
                                        if: System.If(condition = Steps.getAllTemplates.output.result.data.length > 0) AFTER Steps.getAllTemplates.output
                                            true
                                                setStore4: UIEngine.SetStore(path = "Page.showTable", value = "show") AFTER Steps.if.true
                                            false
                                                setStore5: UIEngine.SetStore(path = "Page.showTable", value = "not show") AFTER Steps.if.false
                                        setStore: UIEngine.SetStore(path = "Page.getAllTemplatesError", value = Steps.getAllTemplates.output.error)
                                        setStore1: UIEngine.SetStore(path = "Page.getAllTemplatesData", value = Steps.getAllTemplates.output.result)
                                            output
                                                number: UIEngine.SetStore(path = "Page.number", value = {{ {{Page.number}} ?? 1 }}) AFTER Steps.setStore1.output
                                                camelcase_Convertion: _.camelcase_Convertion() AFTER Steps.setStore1.output