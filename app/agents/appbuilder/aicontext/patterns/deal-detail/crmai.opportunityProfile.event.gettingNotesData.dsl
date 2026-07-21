FUNCTION gettingNotesData
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.filterObject", value = {
    "field": "opportunityId",
    "value": ""
})
            output
                setStore1: UIEngine.SetStore(path = "Page.filterObject.value", value = Page.activeOppDetails._id) AFTER Steps.setStore.output
                    output
                        readPage: CoreServices.Storage.ReadPage(size = 20, appCode = "crmai", filter = Page.filterObject, storageName = "OpportunityNotes") AFTER Steps.setStore1.output
                            output
                                setStore2: UIEngine.SetStore(path = "Page.notesData", value = Steps.readPage.output.result.content)