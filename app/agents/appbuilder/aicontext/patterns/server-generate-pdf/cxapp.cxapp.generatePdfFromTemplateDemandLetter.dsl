FUNCTION generatePdfFromTemplateDemandLetter
    NAMESPACE cxapp
    PARAMETERS
        appCode AS {"type": "STRING", "version": 1}
        fileLocation AS {"type": "STRING", "version": 1}
        fileName AS {"type": "STRING", "version": 1}
        fileOverride AS {"type": "BOOLEAN", "version": 1}
        fileType AS {"type": "STRING", "version": 1}
        templateData AS {"type": "OBJECT", "version": 1}
        templateName AS {"type": "STRING", "version": 1}
        clientCode AS {}
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
}, eventName = "result")