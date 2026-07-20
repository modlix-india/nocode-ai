FUNCTION download_master_plan
    LOGIC
        if: System.If(condition = Page.project.newPlp.documents.floorPlan[0].imageUrl)
            true
                sendData: UIEngine.SendData(url = Page.project.newPlp.documents.floorPlan[0].imageUrl, method = "GET", queryParams = {
    "download": {
        "value": true
    }
}, downloadAsAFile = true) AFTER Steps.if.true
                    error
                        message: UIEngine.Message(msg = Steps.sendData.error.data)