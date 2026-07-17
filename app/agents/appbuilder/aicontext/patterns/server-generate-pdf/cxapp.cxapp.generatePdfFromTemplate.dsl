FUNCTION generatePdfFromTemplate
    NAMESPACE cxapp
    PARAMETERS
        clientCode AS {"type": "STRING", "version": 1}
        fileName AS {"type": "STRING", "version": 1}
        templateName AS {"type": "STRING", "version": 1}
        templateData AS {"type": "OBJECT", "version": 1, "defaultValue": {}}
        fileOverride AS {"defaultValue": false, "version": 1, "type": "BOOLEAN"}
        fileLocation AS {"type": "STRING", "version": 1, "defaultValue": "/"}
        appCode AS {"type": "STRING", "version": 1}
        fileType AS {"defaultValue": "static", "version": 1, "type": "STRING"}
    EVENTS
        result
            response AS {"type": "OBJECT", "version": 1}
    LOGIC
        templateToPdf: CoreServices.File.TemplateToPdf(templateName = Arguments.templateName, clientCode = Arguments.clientCode, appCode = Arguments.appCode, fileName = Arguments.fileName, templateData = Arguments.templateData, fileOverride = Arguments.fileOverride, fileType = Arguments.fileType, fileLocation = Arguments.fileLocation)
            output
                generateEvent: System.GenerateEvent(results = {
    "name": "response",
    "value": {
        "isExpression": true,
        "value": "Steps.templateToPdf.output.fileData"
    }
}, eventName = "result") AFTER Steps.templateToPdf.output