FUNCTION uploadIcon
    LOGIC
        uploadEvent: UIEngine.SendData(url = `'api/files/static/'`, method = "POST", headers = {
    "Authorization": {
        "location": {
            "expression": "LocalStore.AuthToken",
            "type": "EXPRESSION"
        }
    },
    "clientCode": {
        "location": {
            "expression": "Store.auth.loggedInClientCode",
            "type": "EXPRESSION"
        }
    },
    "content-type": {
        "value": "multipart/form-data"
    }
}, payload = Page.selectedFilesForUpload, queryParams = {})
            error
                message: UIEngine.Message(msg = Steps.uploadEvent.error.data)
            output
                setStore: UIEngine.SetStore(path = "Page.preview", value = Steps.uploadEvent.output.data.url)
                forEachLoop: System.Loop.ForEachLoop(source = [{
    "size": "48"
}, {
    "size": "72"
}, {
    "size": "96"
}, {
    "size": "144"
}, {
    "size": "168"
}, {
    "size": "192"
}, {
    "size": "256"
}, {
    "size": "512"
}]) AFTER Steps.uploadEvent.output
                    iteration
                        Icons: UIEngine.SetStore(path = `'Page.manifest.icons[{{Steps.forEachLoop.iteration.index}}].src'`, value = `"/{{Steps.uploadEvent.output.data.url}}?height={{Steps.forEachLoop.iteration.each.size}}&width={{Steps.forEachLoop.iteration.each.size}}&keepAspectRatio=false"`)
                        Icons_2: UIEngine.SetStore(path = `'Page.manifest.icons[{{Steps.forEachLoop.iteration.index}}].type'`, value = Steps.uploadEvent.output.data.type)
                        Icons_1: UIEngine.SetStore(path = `'Page.manifest.icons[{{Steps.forEachLoop.iteration.index}}].size'`, value = `'{{Steps.forEachLoop.iteration.each.size}}x{{Steps.forEachLoop.iteration.each.size}}'`)
                    output
                        save_Function: _.save_Function() AFTER Steps.forEachLoop.output