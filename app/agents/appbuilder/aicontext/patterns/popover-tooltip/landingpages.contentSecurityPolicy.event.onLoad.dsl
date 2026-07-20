FUNCTION onLoad
    LOGIC
        initializeData: _.initializeData()
        fetchData: UIEngine.FetchData(url = `'api/ui/applications?appCode={{Store.urlDetails.pathParts[1]}}'`)
            error
                messageFetchStep_Copy_1: UIEngine.Message(msg = Steps.fetchData.error.data)
            output
                fetchData1: UIEngine.FetchData(url = `'api/ui/applications/{{Steps.fetchData.output.data.content[0].id}}'`)
                    error
                        messageFetchStep: UIEngine.Message(msg = Steps.fetchData1.error.data)
                    output
                        setStore: UIEngine.SetStore(path = "Page.appdef", value = Steps.fetchData1.output.data)
                            output
                                setStore1: UIEngine.SetStore(path = "Page.originalCspReport", value = Page.appdef.properties.cspReport) AFTER Steps.setStore.output
                                    output
                                        setCspReportDetails: _.setCspReportDetails() AFTER Steps.setStore1.output
                                            output
                                                setStore1_Copy_1: UIEngine.SetStore(path = "Page.originalCsp", value = Page.appdef.properties.csp) AFTER Steps.setCspReportDetails.output
                                                    output
                                                        setCspDetails: _.setCspDetails() AFTER Steps.setStore1_Copy_1.output