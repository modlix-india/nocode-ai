FUNCTION deleteScheduleCallById
    NAMESPACE hrms
    PARAMETERS
        _id AS {"type": "STRING", "version": 1}
    EVENTS
        output
            response AS {"type": "OBJECT", "version": 1}
        error
            message AS {"type": "STRING", "version": 1}
    LOGIC
        delete: CoreServices.Storage.Delete(appCode = "hrms", storageName = "ScheduleCallDetails", dataObjectId = Arguments._id)
            error
                generateEvent1: System.GenerateEvent(eventName = "error", results = {
    "name": "message",
    "value": {
        "isExpression": true,
        "value": "Steps.delete.error.result"
    }
})
            output
                generateEvent: System.GenerateEvent(results = {
    "name": "response",
    "value": {
        "isExpression": true,
        "value": "Steps.delete.output.result"
    }
})