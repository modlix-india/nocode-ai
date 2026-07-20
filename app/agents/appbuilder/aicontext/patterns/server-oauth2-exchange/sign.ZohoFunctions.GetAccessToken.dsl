FUNCTION GetAccessToken
    NAMESPACE ZohoFunctions
    EVENTS
        output
            accessToken AS <REDACTED>
            expiresIn AS {"type": "INTEGER", "version": 1, "description": "Zoho provides Expires in Seconds."}
        errorOutput
            error AS {"type": "STRING", "version": 1}
    LOGIC
        postRequest: CoreServices.REST.PostRequest(connectionName = "ZohoRestAuthToken", queryParams = {
    "client_id": "1000.93VGFOV75S26F0KY5QLIYT42UY15PQ",
    "client_secret": "<REDACTED>",
    "grant_type": "client_credentials",
    "scope": "ZohoSign.documents.ALL,ZohoSign.templates.ALL",
    "soid": "ZohoSign.<PHONE>"
}, url = "/oauth/v2/token", payload = {}, appCode = "sign")
            output
                if: System.If(condition = Steps.postRequest.output.data.error !=null)
                    true
                        errorEvent: System.GenerateEvent(results = {
    "name": "error",
    "value": {
        "isExpression": true,
        "value": "Steps.postRequest.output.data.error"
    }
}, eventName = "errorOutput") AFTER Steps.if.true
                    false
                        outputEvent: System.GenerateEvent(results = {
    "name": "accessToken",
    "value": {
        "isExpression": true,
        "value": "Steps.postRequest.output.data.access_token"
    }
}, results = {
    "name": "expiresIn",
    "value": {
        "isExpression": true,
        "value": "Steps.postRequest.output.data.expires_in"
    }
}) AFTER Steps.if.false