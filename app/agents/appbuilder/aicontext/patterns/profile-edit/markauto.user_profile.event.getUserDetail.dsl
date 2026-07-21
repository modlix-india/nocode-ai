FUNCTION getUserDetail
    NAMESPACE UIEngine
    EVENTS
        output
            userDetailData AS {"name": "userDetailData", "type": "Object"}
    LOGIC
        getData: UIEngine.FetchData(url = "api/core/data/UserStorage/useid", pathParams = {
    "useid": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Store.urlDetails.pathParts[1]??''"
        }
    }
})
            output
                setUserDetailsData: UIEngine.SetStore(path = "Page.user", value = Steps.getData.output.data) AFTER Steps.getData.output
                    output
                        genOutput: System.GenerateEvent(eventName = "output", results = {
    "name": "userData",
    "value": {
        "isExpression": true,
        "value": "Steps.setUserDetailsData.output.data"
    }
})