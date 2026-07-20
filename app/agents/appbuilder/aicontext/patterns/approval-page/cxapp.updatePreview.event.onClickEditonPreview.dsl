FUNCTION onClickEditonPreview
    LOGIC
        fetching_phaseDetails: _.fetching_phaseDetails()
            output
                setStore1: UIEngine.SetStore(path = "Page.editUpdate", value = Page.previewData) AFTER Steps.fetching_phaseDetails.output
                    output
                        setStore: UIEngine.SetStore(path = "Page.screenVisibility", value = "EDIT") AFTER Steps.setStore1.output
                            output
                                read: CoreServices.Storage.Read(appCode = "rim", storageName = "Phase", dataObjectId = Page.particularPhaseId) AFTER Steps.setStore.output
                                    output
                                        setStore2: UIEngine.SetStore(path = "Page.phaseData", value = Steps.read.output.result)
                                            output
                                                fetching_towerDetails: _.fetching_towerDetails() AFTER Steps.setStore2.output
                                                    output
                                                        read1: CoreServices.Storage.Read(appCode = "rim", storageName = "Tower", dataObjectId = Page.particularTowerId) AFTER Steps.fetching_towerDetails.output
                                                            output
                                                                setStore5: UIEngine.SetStore(path = "Page.towerData", value = Steps.read1.output.result)