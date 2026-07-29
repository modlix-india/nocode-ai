FUNCTION onclick_sign_EOI
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.currentDocument", value = Parent)
            output
                setStore1: UIEngine.SetStore(path = "Page.projectName", value = Page.projects.{{Page.currentDocument.projectId}}.projectFullName) AFTER Steps.setStore.output
                    output
                        setStore2: UIEngine.SetStore(path = "Page.bookingId", value = Page.currentDocument._id) AFTER Steps.setStore1.output
                            output
                                setStore16: UIEngine.SetStore(path = "Page.kycAccountType", value = `Page.kycs.{{Page.currentDocument.kycAccountId}}.individual != undefined ? 'individual' : Page.kycs.{{Page.currentDocument.kycAccountId}}.joint != undefined ? 'joint' : Page.kycs.{{Page.currentDocument.kycAccountId}}.huf != undefined ? 'huf' : Page.kycs.{{Page.currentDocument.kycAccountId}}.llp != undefined ? 'llp' : Page.kycs.{{Page.currentDocument.kycAccountId}}.private != undefined ? 'private' : Page.kycs.{{Page.currentDocument.kycAccountId}}.trust != undefined ? 'trust' : Page.kycs.{{Page.currentDocument.kycAccountId}}.partnership != undefined ? 'partnership' : ''`) AFTER Steps.setStore2.output
                                    output
                                        eoiInternal: UIEngine.SetStore(path = "Page.navigationUrlEoiInternal", value = `Page.currentDocument.documents.eoi.documentId  ?   '/SignEOI/{{Page.bookingId}}/eoiInternal/{{Page.currentDocument.documents.eoi.documentId}}/{{Page.projects.{{Page.currentDocument.projectId}}.projectFullName}}/{{Page.currentDocument.kycAccountId}}/{{Page.kycAccountType}} '  :   '/SignEOI/{{Page.bookingId}}/eoiInternal/{{Page.currentDocument.documents.eoi._id}}/{{Page.projects.{{Page.currentDocument.projectId}}.projectFullName}}/{{Page.currentDocument.kycAccountId}}/{{Page.kycAccountType}}'`) AFTER Steps.setStore16.output
                                            output
                                                eoii: UIEngine.SetStore(path = "Page.navigationUrlEoi", value = `Page.currentDocument.documents.eoi.documentId   ?   '/SignEOI/{{Page.bookingId}}/eoi/{{Page.currentDocument.documents.eoi.documentId}}/{{Page.projects.{{Page.currentDocument.projectId}}.projectFullName}}/{{Page.currentDocument.kycAccountId}}/{{Page.kycAccountType}}'     :     '/SignEOI/{{Page.bookingId}}/eoi/{{Page.currentDocument.documents.eoi._id}}/{{Page.projects.{{Page.currentDocument.projectId}}.projectFullName}}/{{Page.currentDocument.kycAccountId}}/{{Page.kycAccountType}}'`) AFTER Steps.eoiInternal.output
                                                    output
                                                        if: System.If(condition = Page.currentDocument.documents.eoi.isInternal) AFTER Steps.eoii.output
                                                            true
                                                                navigate: UIEngine.Navigate(linkPath = Page.navigationUrlEoiInternal) AFTER Steps.if.true
                                                            false
                                                                navigate_Copy_1: UIEngine.Navigate(linkPath = Page.navigationUrlEoi) AFTER Steps.if.false