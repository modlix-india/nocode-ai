FUNCTION download_floor_plan
    LOGIC
        sendData: UIEngine.SendData(url = Page.project.newPlp.documents.masterPlan[0].imageUrl, method = "GET", queryParams = {
    "download": {
        "value": true
    }
}, downloadAsAFile = true)
            error
                message: UIEngine.Message(msg = Steps.sendData.error.data)