FUNCTION download_master_plan
    LOGIC
        sendData: UIEngine.SendData(url = Page.project.newPlp.documents.floorPlan[0].imageUrl, method = "GET", queryParams = {
    "download": {
        "value": true
    }
}, downloadAsAFile = true)
            error
                message: UIEngine.Message(msg = Steps.sendData.error.data)