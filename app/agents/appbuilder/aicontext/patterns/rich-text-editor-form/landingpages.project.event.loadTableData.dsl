FUNCTION loadTableData
    LOGIC
        loadPages: _.loadPages()
        fetchData: UIEngine.FetchData(url = `'/api/security/applications/applyAppCodeSuffix?appCode={{Store.urlDetails.pathParts[1]}}'`)
            output
                setStore: UIEngine.SetStore(path = "Page.urlPrefix", value = Steps.fetchData.output.data)