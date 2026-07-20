FUNCTION OnClickCreateLeadCloseButton
    LOGIC
        updatingpopup: UIEngine.SetStore(path = "Page.newForm", value = false)
            output
                updatingToEmpty: UIEngine.SetStore(path = `'Page.Data'`, value = []) AFTER Steps.updatingpopup.output
                    output
                        updatingtoIntialValue: UIEngine.SetStore(path = `'Page.index'`, value = 0) AFTER Steps.updatingToEmpty.output
                            output
                                updatingToepmty: UIEngine.SetStore(path = `'Page.leadForm'`, value = {}) AFTER Steps.updatingtoIntialValue.output
                                    output
                                        uodatingFileEmpty: UIEngine.SetStore(path = "Page.bgImageUpload.file", value = "") AFTER Steps.updatingToepmty.output
                                            output
                                                updatingToBgImgEmpty: UIEngine.SetStore(path = "Page.imagePreview", value = "") AFTER Steps.uodatingFileEmpty.output